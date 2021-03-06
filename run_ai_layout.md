The following "connection steps" are ordered from how the data from the
client that wants to start a game eventually flows to something that actually
starts running the AIs.

In summary, there are four parts, divided across two programs:
  1. Django-land (manage.py runserver)
    * The Websocket interface to the client, 
mainly handled by django_channels
    * The interface the the game scheduler
  2. Game Scheduler (run_gamescheduler_server.py)
    * The receptor asyncio.Protocol to handle connections initiated by Django
    * The process managers for starting AIs in sandboxes while communicating
      with them

Steps 1-4 communicate exclusively with JSON "packets". The format of one of
these packets is listed below:

*TODO* (if you turn on debug logging and play a game, you will be able to see 
the packets yourself in the log)

1.  django_channels
  * Handles incomming connections to Websocket
  * Routes in othello/routing.py

2. othello.apps.games.consumers.GameConsuer
  * Is a subclass of channels.generic.AsyncJsonWebsocketConsumer
  * A new one is created for every new connected client
  * Subclasses are GamePlayingConsumer and GameWatchingConsumer
    * The only difference between them is which data they get from the 
      websocket url, plus a "Watching" isn't supposed to send moves
      or recieve moverequests
  * Creates a TCP connection to the game scheduler server using the
    class described in the next step
  * Passes JSON packets transparently

3. othello.gamescheduler.client.GameSchedulerClient
  * Is a subclass of asyncio.Protocol
  * The factory method passed to asyncio.create_connection creates a new one
    individual to each GameConsumer
  * Passes JSON packets transparently

4. othello.gamescheduler.server.GameScheduler
  * Is a subclass of asyncio.Protocol
    * These are special objects that can be created with asyncio functionality
      whenever a TCP connection is established, neat stuff
  * To keep a consistent state, the factory method passed to
    asycnio.Loop.create_server returns the same object every time
  * Keeps a list of "Rooms" and corresponding IDs
    * Each new client is assigned their own room
    * Each client is immediately sent back their room ID to remember
    * Messages sent to a room are sent to all clients in that room to
      facilitate watching games
    * Only the original client can send messages to their room
  * In an asyncio.ThreadPoolExecutor, starts the next step to actually
    run a game
    * Has a callback for communication from process
    * Has a threadsafe queue for communication to process

5. othello.gamescheduler.worker.GameRunner
  * In charge of handling master state for a game
  * Spawns two of the following step to get moves from the AIs

6. othello.gamescheduler.run_ai_utils.JailedRunnerCommunicator
  * Uses subprocess.Popen and a command configured in settings to spawn the
    next step in a sandbox
  * Uses crazy magic found on stackoverflow to, without blocking, capture
    output

7. othello.gamescheduler.run_ai_utils.JailedRunner
  * Interfaces with previous step over stdout/stdin, passes values to next step
  * Uses crazy magic found on stackoverflow to redirect output that would be
    sent to stdout by the AI to stderr instead

8. othello.apps.games.run_ai_utils.LocalRunner
  * Dynamically imports students.20xxsomeone.strategy.Strategy
  * Every time a move is requested, starts up the AI in a
    multiprocessing.Process, kills it if it's taking too long
