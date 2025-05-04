"""
war card game client and server
"""
import asyncio
from collections import namedtuple
from enum import Enum
import logging
import random
import socket
import socketserver
import threading
import sys
import select
import time

Game = namedtuple("Game", ["p1", "p2"])

waiting_clients = []


class Command(Enum):
    WANTGAME = 0
    GAMESTART = 1
    PLAYCARD = 2
    PLAYRESULT = 3


class Result(Enum):
    WIN = 0
    DRAW = 1
    LOSE = 2

def readexactly(sock, numbytes):
    data = bytearray()
    while len(data) < numbytes:
        chunk = sock.recv(numbytes - len(data))
        if chunk == b'':
            raise ConnectionResetError("peer closed whilst reading")
        data.extend(chunk)
    return bytes(data)


def kill_game(game: Game):
    for side in (game.p1, game.p2):
        try:
            side.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            side.close()
        except OSError:
            pass

def _rank(card: int) -> int:
    return card % 13
def compare_cards(card1: int, card2: int) -> int:
    r1, r2 = _rank(card1), _rank(card2)
    return (r1 > r2) - (r1 < r2)

def deal_cards() -> tuple[list[int], list[int]]:
    deck = list(range(52))
    random.shuffle(deck)
    return deck[:26], deck[26:52]

def handle_game(p1: socket.socket, p2: socket.socket) -> None:
    game = Game(p1, p2)
    try:
        for player in game:
            hdr = readexactly(player, 2)
            if hdr[0] != Command.WANTGAME.value or hdr[1] != 0:
                raise ValueError("expected WANTGAME 0")
        hand1, hand2 = deal_cards()
        p1.sendall(bytes([Command.GAMESTART.value]) + bytes(hand1))
        p2.sendall(bytes([Command.GAMESTART.value]) + bytes(hand2))

        remaining1, remaining2 = set(hand1), set(hand2)

        for _ in range(26):
            msgs = {}
            while len(msgs) < 2:
                r, _, _ = select.select([p1, p2], [], [])
                for ready in r:
                    pkt = readexactly(ready, 2)
                    if pkt[0] != Command.PLAYCARD.value:
                        raise ValueError("expected PLAYCARD")
                    msgs[ready] = pkt[1]
            c1, c2 = msgs[p1], msgs[p2]

            if c1 not in remaining1 or c2 not in remaining2:
                raise ValueError("invalid card played")
            remaining1.remove(c1)
            remaining2.remove(c2)

            cmp = compare_cards(c1, c2)
            if not cmp:
                res1 = res2 = Result.DRAW.value
            elif cmp > 0:
                res1, res2 = Result.WIN.value, Result.LOSE.value
            else:
                res1, res2 = Result.LOSE.value, Result.WIN.value

            p1.sendall(bytes([Command.PLAYRESULT.value, res1]))
            p2.sendall(bytes([Command.PLAYRESULT.value, res2]))
    except (ConnectionResetError, ValueError, OSError) as exc:
        logging.debug("aborting game: %s", exc)
    finally:
        kill_game(game)

def serve_game(host: str, port: int) -> None:
    """
    Listen forever on *host*: *port*, pairing clients in the order they connect
    and launching a new thread for every game.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen()
        logging.info("WAR server listening on %s:%s", host, port)

        try:
            while True:
                client_sock, _ = listener.accept()
                client_sock.setblocking(True)
                waiting_clients.append(client_sock)
                logging.debug("client arrived – waiting queue size %d",
                              len(waiting_clients))
                if len(waiting_clients) >= 2:
                    p1 = waiting_clients.pop(0)
                    p2 = waiting_clients.pop(0)
                    threading.Thread(target=handle_game,
                                     args=(p1, p2),
                                     daemon=True).start()
        finally:
            for lonely in waiting_clients:
                try:
                    lonely.close()
                except OSError:
                    pass
    

async def limit_client(host, port, loop, sem):
    """
    Limit the number of clients currently executing.
    You do not need to change this function.
    """
    async with sem:
        return await client(host, port, loop)

async def client(host, port, loop):
    """
    Run an individual client on a given event loop.
    You do not need to change this function.
    """
    try:
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(b"\0\0")
        card_msg = await reader.readexactly(27)
        myscore = 0
        for card in card_msg[1:]:
            writer.write(bytes([Command.PLAYCARD.value, card]))
            result = await reader.readexactly(2)
            if result[1] == Result.WIN.value:
                myscore += 1
            elif result[1] == Result.LOSE.value:
                myscore -= 1
        if myscore > 0:
            result = "won"
        elif myscore < 0:
            result = "lost"
        else:
            result = "drew"
        logging.debug("Game complete, I %s", result)
        writer.close()
        return 1
    except ConnectionResetError:
        logging.error("ConnectionResetError")
        return 0
    except asyncio.streams.IncompleteReadError:
        logging.error("asyncio.streams.IncompleteReadError")
        return 0
    except OSError:
        logging.error("OSError")
        return 0



def _run_selftest(host: str, port: int, games: int) -> None:
    server_thread = threading.Thread(
        target=serve_game, args=(host, port), daemon=True
    )
    server_thread.start()
    time.sleep(0.3)

    async def counted_client():
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(b"\0\0")
        hand_msg = await reader.readexactly(27)
        my_hand = hand_msg[1:]
        draws = 0
        for card in my_hand:
            writer.write(bytes([Command.PLAYCARD.value, card]))
            res = await reader.readexactly(2)
            if res[1] == Result.DRAW.value:
                draws += 1
        writer.close()
        await writer.wait_closed()
        return draws

    async def run_all():
        tasks = [counted_client() for _ in range(games * 2)]
        return await asyncio.gather(*tasks)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    per_client_draws = loop.run_until_complete(run_all())
    loop.close()

    total_draws   = sum(per_client_draws)
    draws_per_game = total_draws / 2 / games
    pct = (draws_per_game / 26) * 100
    logging.info("Self-test complete: %d games, %d clients, %d clean exits",
                 games, games * 2, games * 2)
    logging.info("  average draws per game: %.2f   (≈ %.2f %%)",
                 draws_per_game, pct)



def main(args):
    """
    launch a client/server
    """
    host = args[1]
    port = int(args[2])
    if args[0] == "selftest":
        games = int(args[3]) if len(args) > 3 else 100
        _run_selftest(host, port, games)
        return
    if args[0] == "server":
        try:
            serve_game(host, port)
        except KeyboardInterrupt:
            pass
        return
    else:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
        
        asyncio.set_event_loop(loop)
        
    if args[0] == "client":
        loop.run_until_complete(client(host, port, loop))
    elif args[0] == "clients":
        sem = asyncio.Semaphore(1000)
        num_clients = int(args[3])
        clients = [limit_client(host, port, loop, sem)
                   for x in range(num_clients)]
        async def run_all_clients():
            """
            use `as_completed` to spawn all clients simultaneously
            and collect their results in arbitrary order.
            """
            completed_clients = 0
            for client_result in asyncio.as_completed(clients):
                completed_clients += await client_result
            return completed_clients
        res = loop.run_until_complete(
            asyncio.Task(run_all_clients(), loop=loop))
        logging.info("%d completed clients", res)

    loop.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    main(sys.argv[1:])