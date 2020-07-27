#!/usr/bin/env python

import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.parse as urlparse
import json

import cnc.logging_config as logging_config
from cnc.gcode import GCode, GCodeException
from cnc.gmachine import GMachine, GMachineException

machine = GMachine()


def do_line(line):
    try:
        g = GCode.parse_line(line)
        res = machine.do_command(g)
    except (GCodeException, GMachineException) as e:
        print('ERROR ' + str(e))
        return False
    if res is not None:
        return 'OK ' + res
    else:
        return 'OK'
    return True


class GetHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed_path = urlparse.urlparse(self.path)
        # print(f"QUERY: {parsed_path.query}")
        query = urlparse.parse_qs(parsed_path.query)
        command = query["com"][0]
        
        response = do_line(command)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(bytes(response, "utf-8"))
        return

def main():
    logging_config.debug_disable()
    try:
        PORT = 10913
        server = HTTPServer(('localhost', PORT), GetHandler)
        print(f'Starting server at http://localhost:{PORT}')
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("\r\nExiting...")
    machine.release()


if __name__ == "__main__":
    main()
