"""In-browser remote desktop: credential resolution, tickets, and the guacd relay.

The console is a thin, authenticated path from a browser to a deployed guest's
RDP service, with ``guacd`` (Apache Guacamole's proxy daemon) doing the protocol
translation. Deliberately *only* guacd: there is no ``guacamole-client`` webapp,
no second database, and no second set of accounts, so the session JWT the app
already issues stays the only credential in the system.

Three modules, in request order:

* ``credentials`` — which Windows account a given VM's session signs in as, read
  out of that VM's ``vm_registry`` row. Pure.
* ``tickets`` — a short-lived, single-use Valkey handoff between the HTTP route
  that authorizes a session and the WebSocket that opens it. Browsers cannot set
  headers on a WS upgrade, and the credentials must not ride in a query string.
* ``guacd`` — the Guacamole protocol client and the byte relay.
"""
