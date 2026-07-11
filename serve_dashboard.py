import http.server
import socket
import sys
import traceback

PORT = 8081
DIRECTORY = r"C:\ev vechile\dashboard"

class DualStackServer(http.server.ThreadingHTTPServer):
    def server_bind(self):
        try:
            # Tell the socket to support both IPv4 and IPv6 dual-stack connections
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except Exception:
            pass
        super().server_bind()

class SafeHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)
        
    def log_message(self, format, *args):
        # Clean logging output
        print(f"[HTTP] {format % args}")

if __name__ == "__main__":
    print(f"Initializing Dual-Stack web server on port {PORT} for directory: {DIRECTORY}")
    # Bind to standard IPv6 wildcard address
    server_address = ("localhost", PORT)
    
    # Use AF_INET6 address family to enable the dual-stack listener
    DualStackServer.address_family = socket.AF_INET6
    DualStackServer.allow_reuse_address = True
    
    try:
        with DualStackServer(("", PORT), SafeHandler) as httpd:
            print(f"Web server running on port {PORT}...")
            sys.stdout.flush()
            httpd.serve_forever()
    except Exception as e:
        print(f"Fatal server crash: {e}")
        traceback.print_exc()
