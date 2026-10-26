#!/usr/bin/python3
"""Bounded IPv4 UDP/TCP DNS relay; no query logging or persistent cache."""

import concurrent.futures
import http.client
import logging
import signal
import socket
import socketserver
import struct
import sys
import threading


HOST = None
POOL = None
SLOTS = threading.BoundedSemaphore(128)
IP_PKTINFO = 8


def failure(query):
    if len(query) < 12:
        return b""
    flags = (struct.unpack("!H", query[2:4])[0] & 0x7900) | 0x8082
    return query[:2] + struct.pack("!H", flags) + query[4:6] + b"\0" * 6 + query[12:]


def answer(query):
    connection = http.client.HTTPConnection(HOST, 18080, timeout=7)
    try:
        if len(query) < 12:
            raise ValueError("Short DNS header")
        connection.request("POST", "/__wsl_gsa_dns", query,
                           {"Content-Type": "application/dns-message"})
        response = connection.getresponse()
        data = response.read(65536)
        if response.status != 200 or not 12 <= len(data) <= 65535 or data[:2] != query[:2]:
            raise ValueError("Invalid Windows DNS reply")
        return data
    except (OSError, ValueError, http.client.HTTPException) as error:
        logging.error("DNS forwarding failed (%s)", type(error).__name__)
        return failure(query)
    finally:
        connection.close()


class BoundedServer:
    def process_request(self, request, address):
        if not SLOTS.acquire(blocking=False):
            logging.error("DNS request queue full")
            self.shutdown_request(request)
            return
        POOL.submit(self.worker, request, address)

    def worker(self, request, address):
        try:
            self.finish_request(request, address)
        except Exception as error:
            logging.error("DNS handler failed (%s)", type(error).__name__)
        finally:
            self.shutdown_request(request)
            SLOTS.release()


class UDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        query, sock, packet_info = self.request
        data = answer(query)
        if data:
            # Preserve the destination address so reverse NAT accepts connected UDP replies.
            sock.sendmsg([data], packet_info, 0, self.client_address)


def read_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


class TCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        try:
            while True:
                size = struct.unpack("!H", read_exact(self.request, 2))[0]
                data = answer(read_exact(self.request, size))
                self.request.sendall(struct.pack("!H", len(data)) + data)
        except EOFError:
            return
        except (OSError, ValueError) as error:
            logging.error("TCP DNS failed (%s)", type(error).__name__)


class UDPServer(BoundedServer, socketserver.UDPServer):
    max_packet_size = 65535

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IP, IP_PKTINFO, 1)
        super().server_bind()

    def get_request(self):
        data, ancillary, _, address = self.socket.recvmsg(65535, socket.CMSG_SPACE(12))
        info = [(level, kind, value) for level, kind, value in ancillary
                if level == socket.IPPROTO_IP and kind == IP_PKTINFO]
        return (data, self.socket, info), address


class TCPServer(BoundedServer, socketserver.TCPServer):
    allow_reuse_address = True
    request_queue_size = 64


def stop(signum, frame):
    raise SystemExit(0)


def main():
    global HOST, POOL
    HOST = sys.argv[1]
    POOL = concurrent.futures.ThreadPoolExecutor(max_workers=32)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with UDPServer(("0.0.0.0", 1053), UDPHandler) as udp, \
            TCPServer(("0.0.0.0", 1053), TCPHandler) as tcp:
        threading.Thread(target=tcp.serve_forever, daemon=True).start()
        logging.info("DNS bridge ready")
        try:
            udp.serve_forever()
        finally:
            POOL.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
