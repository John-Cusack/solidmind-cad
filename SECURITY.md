# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in SolidMind CAD, please report it responsibly.

**Email:** security@solidmind.dev

Please include:
- A description of the vulnerability
- Steps to reproduce
- Potential impact

We will acknowledge your report within 48 hours and aim to provide a fix or mitigation within 7 days for critical issues.

## Scope

SolidMind CAD runs as a local development tool. The MCP bridge server and FreeCAD addon communicate over localhost TCP sockets (ports 9876-9879). These are not designed to be exposed to untrusted networks.

**In scope:**
- Code execution via MCP tool inputs
- Path traversal in file export/import operations
- Vulnerabilities in the socket protocol handling

**Out of scope:**
- Issues that require the attacker to already have local access to the machine
- Denial of service against the localhost socket servers

## Security Considerations

- The socket servers bind to `localhost` only and are intended for local IPC
- File export paths are user-provided — avoid exposing the server to untrusted inputs
- The `exec(open(...).read())` pattern in `freecad_addon/test_integration.py` is for test use only
