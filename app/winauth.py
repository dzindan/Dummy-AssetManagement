"""Signing in with AD accounts (client/server mode, 2026-11-02): the app
runs on a domain-joined server, so Windows itself checks who someone is -
no password goes over the network in clear text, no LDAP needed:

- Windows sign-in (SSO): the browser sends the logged-on Windows user
  (HTTP "Negotiate" = Kerberos, or NTLM) - the person types nothing.
  Server side: SSPI (AcceptSecurityContext) via pywin32's `sspi`.
- The form: DOMAIN\\user (or user@domain, or just user with the default
  domain) + the AD password, checked with LogonUser.

Either way the result is a Windows token: the user's SID / name and every
group they're in (nested ones too, as Windows expands them). Access = a
member of the AD group set in Users & Roles > AD sign-in (like AAL / SG:
the admin grants it by group membership); the role is given in the app.

Only on Windows with pywin32; everywhere else `available()` is False and
the app keeps its own accounts only.
"""

from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass, field

SSO_PENDING_SECONDS = 60  # an unfinished NTLM handshake is dropped after this


class AuthError(Exception):
    pass


@dataclass
class Identity:
    sid: str
    domain: str
    sam: str
    group_sids: set = field(default_factory=set)

    @property
    def username(self) -> str:
        return f"{self.domain}\\{self.sam}".upper()


def available() -> bool:
    try:
        import sspi  # noqa: F401
        import win32security  # noqa: F401
    except ImportError:
        return False
    return True


def split_name(text: str, default_domain: str) -> tuple[str, str]:
    """'CORP\\nva' / 'nva@corp.local' / 'nva' -> (domain, user)."""
    text = (text or "").strip()
    if "\\" in text:
        domain, user = text.split("\\", 1)
        return domain.strip(), user.strip()
    if "@" in text:
        return "", text  # a UPN: LogonUser takes it whole, with no domain
    return (default_domain or "").strip(), text


def _identity_from_token(token) -> Identity:
    import win32security

    sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    sam, domain, _kind = win32security.LookupAccountSid(None, sid)
    groups = win32security.GetTokenInformation(token, win32security.TokenGroups)
    return Identity(sid=win32security.ConvertSidToStringSid(sid), domain=domain, sam=sam,
                    group_sids={win32security.ConvertSidToStringSid(g[0]) for g in groups})


def logon(name: str, password: str, default_domain: str) -> Identity:
    """The form: check DOMAIN\\user + password with Windows (LogonUser,
    a network logon - nothing is stored, no profile loaded)."""
    import pywintypes
    import win32con
    import win32security

    domain, user = split_name(name, default_domain)
    if not user or not password:
        raise AuthError("Enter your AD username and password.")
    try:
        token = win32security.LogonUser(user, domain or None, password, win32con.LOGON32_LOGON_NETWORK,
                                        win32con.LOGON32_PROVIDER_DEFAULT)
    except pywintypes.error as e:
        # 1326 wrong name / password, 1327-1331 restrictions, 1909 locked out, 1793 expired
        messages = {1326: "Incorrect username or password.", 1909: "This AD account is locked out.",
                    1331: "This AD account is disabled.", 1793: "This AD account has expired.",
                    1907: "The AD password must be changed first (sign in to Windows once).",
                    1330: "The AD password has expired - change it in Windows first."}
        raise AuthError(messages.get(e.winerror, f"Windows refused the sign-in: {e.strerror}")) from None
    try:
        return _identity_from_token(token)
    finally:
        token.Close()


def group_sid(name: str) -> str:
    """'CORP\\AM Users' -> its SID string (AuthError when AD doesn't know it)."""
    import pywintypes
    import win32security

    try:
        sid, _domain, _kind = win32security.LookupAccountName(None, name.strip())
    except pywintypes.error as e:
        raise AuthError(f"AD group not found: {name} ({e.strerror})") from None
    return win32security.ConvertSidToStringSid(sid)


def computer_domain() -> str:
    """The server's own domain (NetBIOS name) - the default for 'user' alone.

    The NetBIOS name is the workstation's LAN group, not the first label of
    the DNS domain: they often differ (shinhan.com.vn vs SHBVN), and
    LogonUser with a wrong domain says only "incorrect password". Off a
    domain (no DNS domain): the computer's own name, for its local accounts."""
    try:
        import win32api
        import win32con
        import win32net
        if win32api.GetComputerNameEx(win32con.ComputerNamePhysicalDnsDomain):
            return (win32net.NetWkstaGetInfo(None, 100)["langroup"] or "").upper() \
                or win32api.GetDomainName().upper()
        return win32api.GetDomainName().upper()
    except Exception:  # noqa: BLE001 - not on a domain / no pywin32
        import os
        return os.environ.get("USERDOMAIN", "").upper()


class NegotiateServer:
    """Windows sign-in over HTTP: the browser answers a 401 'Negotiate' with
    a Kerberos ticket (one round) or NTLM (two rounds, on the same
    connection - each handshake is kept per client address + port until it
    ends or SSO_PENDING_SECONDS pass)."""

    def __init__(self):
        self._pending: dict[tuple, tuple] = {}
        self._lock = threading.Lock()

    def step(self, connection: tuple, header: str) -> tuple[Identity | None, str]:
        """One 'Authorization: Negotiate <token>' -> (Identity when done,
        the token to send back in 'WWW-Authenticate: Negotiate <token>' or '')."""
        import pywintypes
        import sspi

        if not header.lower().startswith("negotiate "):
            raise AuthError("Not a Negotiate token")
        try:
            data = base64.b64decode(header.split(" ", 1)[1].strip())
        except ValueError:
            raise AuthError("Unreadable sign-in token") from None
        now = time.monotonic()
        with self._lock:
            for key in [k for k, (_, t) in self._pending.items() if now - t > SSO_PENDING_SECONDS]:
                del self._pending[key]
            server = self._pending.pop(connection, (None, 0))[0] or sspi.ServerAuth("Negotiate")
        try:
            err, out = server.authorize(data)
        except pywintypes.error as e:
            raise AuthError(f"Windows sign-in failed: {e.strerror}") from None
        reply = base64.b64encode(out[0].Buffer).decode("ascii") if out and out[0].Buffer else ""
        if err:  # SEC_I_CONTINUE_NEEDED: the next round comes on this connection
            with self._lock:
                self._pending[connection] = (server, now)
            return None, reply
        token = server.ctxt.QuerySecurityContextToken()
        try:
            return _identity_from_token(token), reply
        finally:
            token.Close()


negotiate = NegotiateServer()
