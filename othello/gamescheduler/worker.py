import logging
import sys, os, io
import shlex, traceback
import multiprocessing as mp
import subprocess
import time
from threading import Lock
from queue import Empty

from .run_ai_utils import JailedRunnerCommunicator
from .othello_admin import Strategy
from .othello_core import BLACK, WHITE, EMPTY, OUTER
from .utils import get_possible_strats
from .settings import OTHELLO_AI_HUMAN_PLAYER

log = logging.getLogger(__name__)

class GameRunner:
    def __init__(self, black, white, timelimit, loop, room_id, emit_callback):
        self.black = black
        self.white = white
        self.timelimit = timelimit
        self.room_id = room_id
        self.loop = loop
        self.emit_callback = emit_callback

        self.possible_names = get_possible_strats()
        self.strats = dict()
        self.do_quit = False
        self.do_quit_lock = Lock()
        self.has_cleanuped = False

    def emit(self, data):
        log.debug("GameRunner emmitting {}".format(data))
        data['room_id'] = self.room_id
        self.loop.call_soon_threadsafe(self.emit_callback, data)

    def run(self, in_q):
        try:
            self._run(in_q)
        except:
            log.error(traceback.format_exc())
        finally:
            log.debug("Cleaning up after _run")
            self.cleanup()

    def _run(self, in_q):
        """
        Main loop used to run the game in.
        Does not have multiprocess support, expects to be run inside a thread
        """
        # NOTE: on any exit from this function, you MUST call self.cleanup()
        log.debug("GameRunner started to run {} vs {} ({})".format(
            self.black,
            self.white,
            self.timelimit
        ))

        self.strats = dict()
        do_start_game = True

        if self.black not in self.possible_names:
            if self.black == OTHELLO_AI_HUMAN_PLAYER:
                self.strats[BLACK] = None
            else:
                self.emit({
                    "type": "game.error",
                    "error": "{} is not a valid AI name".format(self.black)
                })
                do_start_game = False
        else:
            strat = JailedRunnerCommunicator(self.black)
            strat.start()
            self.strats[BLACK] = strat

        if self.white not in self.possible_names:
            if self.white == OTHELLO_AI_HUMAN_PLAYER:
                self.strats[WHITE] = None
            else:
                self.emit({
                    "type": "game.error",
                    "error": "{} is not a valid AI name".format(self.white)
                })
                do_start_game = False
        else:
            strat = JailedRunnerCommunicator(self.white)
            strat.start()
            self.strats[WHITE] = strat

        log.debug("Inited strats")

        if not do_start_game:
            log.warn("Already threw error, not starting game")
            self.cleanup()
            return

        core = Strategy()
        player = BLACK
        board = core.initial_board()
        names = {
            BLACK: self.black,
            WHITE: self.white,
        }

        # first check to see if we should still emit
        with self.do_quit_lock:
            if self.do_quit:
                log.debug("Quitting early")
                self.cleanup()
                return

        self.emit({
            "type": "board.update",
            "board": ''.join(board),
            "tomove": BLACK,
            "black": names[BLACK],
            "white": names[WHITE],
        })
        forfeit = False

        log.debug("All initing done, time to start playing the game")

        while player is not None and not forfeit:
            # another check before each tick
            with self.do_quit_lock:
                if self.do_quit:
                    log.debug("Quitting while running game")
                    self.cleanup()
                    return
            player, board, forfeit = self.do_game_tick(in_q, core, board, player, names)

        winner = EMPTY
        if forfeit:
            winner = core.opponent(player)
        else:
            winner = (EMPTY, BLACK, WHITE)[core.final_value(BLACK, board)]


        # final check before game ends (just in case ya know)
        with self.do_quit_lock:
            if self.do_quit:
                log.debug("Quit at final check")
                self.cleanup()
                return

        self.emit({
            "type": "board.update",
            "board": ''.join(board),
            "tomove": EMPTY,
            "black": names[BLACK],
            "white": names[WHITE],
        })
        self.emit({
            "type": "game.end",
            "winner": winner,
            "board": ''.join(board),
            "forfeit": forfeit,
        })

        log.debug("Game over, exiting...")
        self.cleanup(False)


    def cleanup(self, messy_end=True):
        # NOTE: putting this in here b/c otherwise, caller doesn't know we
        # errored in a wierd place
        if messy_end and not self.has_cleanuped:
            self.emit({
                "type": "game.error",
                "error": "GameRunner Thread asking to be cleaned up"
            })
            self.emit({
                "type": "game.end",
                "winner": OUTER,
                "board": "",
                "forfeit": False
            })

        if getattr(self.strats.get(BLACK, None), "stop", False):
            self.strats[BLACK].stop()
            log.debug("successfully stopped BLACK jailed runner")
        if getattr(self.strats.get(WHITE, None), "stop", False):
            self.strats[WHITE].stop()
            log.debug("successfully stopped WHITE jailed runner")

        self.has_cleanuped = True

    # just in case of wonkiness
    def __del__(self):
        self.cleanup(False)

    def do_game_tick(self, in_q, core, board, player, names):
        """
        Runs one move in a game, handling all the board flips and game-ending edge cases.

        If a strat is `None`, it calls out for the user to input a move. Otherwise, it runs the strategy provided.
        """
        log.debug("Ticking game")
        strat = self.strats[player]
        move = -1
        errs = None
        if strat is None:
            self.emit({"type":"move.request"})
            waiting_for_move = True
            move = None
            # Using a timeout on the move queue so we can detect
            # if we are supposed to stop while we are waiting for the move
            while waiting_for_move:
                try:
                    move = in_q.get(timeout=1)
                    waiting_for_move = False
                except Empty:
                    with self.do_quit_lock:
                        if self.do_quit:
                            log.debug("Quitting in middle of waiting for move")
                            self.cleanup()
                            # Cause the outer loop to continue, but return 
                            # prematurely b/c we are in the process of quitting
                            return player, board, False
        else:
            move, errs = strat.get_move(board, player, self.timelimit)

        if errs:
            self.emit({
                'type': "game.error",
                'error': "{} error on board {}:\n{}\n".format(names[player], ''.join(board), errs)
            })

        if not core.is_legal(move, player, board):
            self.emit({
                'type': "game.error",
                'error': "{}: {} is an invalid move for board {}\n".format(names[player], move, ''.join(board))
            })
            return player, board, True

        board = core.make_move(move, player, board)
        player = core.next_player(board, player)
        self.emit({
            "type": "board.update",
            "board": ''.join(board),
            "tomove": player,
            "black": names[BLACK],
            "white": names[WHITE],
        })
        return player, board, False
