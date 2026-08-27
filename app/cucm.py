"""Live IP-phone inventory via Cisco CUCM's AXL/RisPort70 API - ported from
the standalone "Cisco Phone Grabber" tool. Two independent things happen for
each phone: CUCM's RisPort70 SOAP service (selectCmDevice) says which phones
are currently *registered* (extension/IP/model/description), then this app
talks directly to each phone's own embedded web page over plain HTTP to
scrape its serial number - CUCM itself doesn't expose serial numbers over
this API. Connection details live in the settings table (see db.py), not a
config file, so they're editable from Settings like everything else here.
"""

from __future__ import annotations

import re
import ssl
from concurrent.futures import ThreadPoolExecutor

import requests
from requests import Session
from requests.auth import HTTPBasicAuth
from urllib3 import disable_warnings
from urllib3.exceptions import InsecureRequestWarning
from zeep import Client
from zeep.cache import SqliteCache
from zeep.transports import Transport

from .db import get_connection, get_setting_on, set_setting_on

ssl._create_default_https_context = ssl._create_unverified_context
disable_warnings(InsecureRequestWarning)

# Fallback number-mask prefixes for the auto-split scan (see
# scan_phones_autosplit) when nothing's configured yet - splitting by
# leading digit is a reasonable generic starting point, but the right split
# depends on each site's own dial plan, which is exactly why this is a
# Settings value instead of a hardcoded list.
DEFAULT_SCAN_PREFIXES = ["0*", "1*", "2*", "3*", "4*", "5*", "6*", "7*", "8*", "9*"]

# Config field names - each stored in the settings table under a
# "cucm_" prefix (see db.py's settings table, a flat key/value store
# shared by every feature) so they can't collide with any other
# feature's own settings.
CUCM_SETTING_KEYS = ("ip", "axluser", "axlpassword", "riswsdl", "scan_prefixes")


def get_cucm_config() -> dict:
    """Exactly what's stored in settings, with no fallback/derivation
    applied - what Settings shows/edits, as opposed to _effective_riswsdl()'s
    derived URL when none is explicitly set."""
    conn = get_connection()
    try:
        return {key: get_setting_on(conn, f"cucm_{key}", "") or "" for key in CUCM_SETTING_KEYS}
    finally:
        conn.close()


def save_cucm_config(values: dict) -> None:
    conn = get_connection()
    try:
        for key in CUCM_SETTING_KEYS:
            set_setting_on(conn, f"cucm_{key}", values.get(key, ""))
        conn.commit()
    finally:
        conn.close()


def load_scan_prefixes() -> list[str]:
    raw = get_cucm_config()["scan_prefixes"]
    if not raw.strip():
        return list(DEFAULT_SCAN_PREFIXES)
    return [p.strip() for p in raw.split(",") if p.strip()]


def _effective_riswsdl(config: dict) -> str:
    # A local WSDL file (see README) is only used if one is actually
    # configured - otherwise fetch it live from the CUCM server itself.
    return config["riswsdl"] or "https://{}:8443/realtimeservice2/services/RISService70?wsdl".format(config["ip"])


class CucmNotConfigured(Exception):
    """Raised instead of letting zeep fail confusingly against an empty
    WSDL URL when nobody's filled in Settings > CUCM Connection yet."""


# Get phones info from cucm
def cucm_rt_phones(model=255, name="", num="", ip="", max=1000):
    config = get_cucm_config()
    if not (config["ip"] and config["axluser"] and config["axlpassword"]):
        raise CucmNotConfigured("CUCM connection isn't configured yet - set it up in Settings first.")
    axluser = config["axluser"]
    axlpassword = config["axlpassword"]
    cucmhost = config["ip"]
    riswsdl = _effective_riswsdl(config)
    StateInfo = ""
    if name:
        SelectBy = "Name"
        SelectItems = {"item": name}
    elif num:
        SelectBy = "DirNumber"
        SelectItems = {"item": num}
    elif ip:
        SelectBy = "IPV4Address"
        SelectItems = {"item": ip}
    else:
        SelectBy = "Name"
        SelectItems = {}
    CmSelectionCriteria = {
        "MaxReturnedDevices": max,
        "DeviceClass": "Phone",
        "Model": model,
        "Status": "Registered",
        "SelectBy": SelectBy,
        "SelectItems": SelectItems,
    }
    session = Session()
    session.verify = False
    session.auth = HTTPBasicAuth(axluser, axlpassword)
    transport = Transport(cache=SqliteCache(), session=session, timeout=5)
    client = Client(wsdl=riswsdl, transport=transport)
    client.service._binding_options["address"] = f"https://{cucmhost}:8443/realtimeservice2/services/RISService70"

    # A Fault here (bad credentials, wrong CUCM IP, unreachable server...)
    # is left to propagate rather than swallowed into an empty list - the
    # route layer (cucm_scan.py) turns it into a visible error message, so
    # "wrong password" doesn't look identical to "no phones match".
    resp = client.service.selectCmDevice(CmSelectionCriteria=CmSelectionCriteria, StateInfo=StateInfo)
    result = resp["SelectCmDeviceResult"]["CmNodes"]["item"]
    out = []
    for node in result:
        if node["CmDevices"] is not None:
            for device in node["CmDevices"]["item"]:
                current = {
                    "ip": device["IPAddress"]["item"][0]["IP"],
                    "model": modelname(device["Model"]),
                    "desc": device["Description"],
                    "num": device["DirNumber"].replace("-Registered", ""),
                }
                out.append(current)
    return out


# Internal CUCM model number -> human readable name. Numbers are CUCM's own
# "enterprise phone type" enum (the `Model` field in RisPort70's
# SelectCmDevice response, same numbering as the AXL `tkmodel` enum) - not
# something this app invents, so they can't be guessed or derived, only
# looked up. Sourced from Cisco's own RisPort70 API reference:
# https://developer.cisco.com/docs/sxml/risport70-api/
MODEL_NAMES = {
    1: "Cisco 30 SP+",
    2: "Cisco 12 SP+",
    3: "Cisco 12 SP",
    4: "Cisco 12 S",
    5: "Cisco 30 VIP",
    6: "Cisco 7910",
    7: "Cisco 7960",
    8: "Cisco 7940",
    9: "Cisco 7935",
    10: "Cisco VGC Phone",
    11: "Cisco VGC Virtual Phone",
    12: "Cisco ATA 186",
    15: "EMCC Base Phone",
    20: "SCCP Phone",
    61: "H.323 Phone",
    72: "CTI Port",
    73: "CTI Route Point",
    115: "Cisco 7941",
    119: "Cisco 7971",
    124: "7914 14-Button Line Expansion Module",
    131: "SIP Trunk",
    132: "SIP Gateway",
    134: "Remote Destination Profile",
    253: "SPA8800",
    255: "Unknown",
    302: "Cisco 7985",
    307: "Cisco 7911",
    308: "Cisco 7961G-GE",
    309: "Cisco 7941G-GE",
    335: "Motorola CN622",
    336: "Third-party SIP Device (Basic)",
    348: "Cisco 7931",
    358: "Cisco Unified Personal Communicator",
    365: "Cisco 7921",
    369: "Cisco 7906",
    374: "Third-party SIP Device (Advanced)",
    375: "Cisco TelePresence",
    376: "Nokia S60",
    404: "Cisco 7962",
    412: "Cisco 3951",
    431: "Cisco 7937",
    434: "Cisco 7942",
    435: "Cisco 7945",
    436: "Cisco 7965",
    437: "Cisco 7975",
    446: "Cisco 3911",
    468: "Cisco Unified Mobile Communicator",
    478: "Cisco TelePresence 1000",
    479: "Cisco TelePresence 3000",
    480: "Cisco TelePresence 3200",
    481: "Cisco TelePresence 500-37",
    484: "Cisco 7925",
    493: "Cisco 9971",
    495: "Cisco 6921",
    496: "Cisco 6941",
    497: "Cisco 6961",
    503: "Cisco Unified Client Services Framework",
    505: "Cisco TelePresence 1300-65",
    520: "Cisco TelePresence 1100",
    521: "Transnova S3",
    522: "BlackBerry MVS VoWifi",
    537: "Cisco 9951",
    540: "Cisco 8961",
    547: "Cisco 6901",
    548: "Cisco 6911",
    550: "Cisco ATA 187",
    557: "Cisco TelePresence 200",
    558: "Cisco TelePresence 400",
    562: "Cisco Dual Mode for iPhone",
    564: "Cisco 6945",
    575: "Cisco Dual Mode for Android",
    577: "Cisco 7926",
    580: "Cisco E20",
    582: "Generic Single Screen Room System",
    583: "Generic Multiple Screen Room System",
    584: "Cisco TelePresence EX90",
    585: "Cisco 8945",
    586: "Cisco 8941",
    588: "Generic Desktop Video Endpoint",
    590: "Cisco TelePresence 500-32",
    591: "Cisco TelePresence 1300-47",
    592: "Cisco 3905",
    593: "Cisco Cius",
    594: "VKEM 36-Button Line Expansion Module",
    596: "Cisco TelePresence TX1310-65",
    597: "Cisco TelePresence MCU",
    598: "Ascom IP-DECT Device",
    599: "Cisco TelePresence Exchange System",
    604: "Cisco TelePresence EX60",
    606: "Cisco TelePresence Codec C90",
    607: "Cisco TelePresence Codec C60",
    608: "Cisco TelePresence Codec C40",
    609: "Cisco TelePresence Quick Set C20",
    610: "Cisco TelePresence Profile 42 (C20)",
    611: "Cisco TelePresence Profile 42 (C60)",
    612: "Cisco TelePresence Profile 52 (C40)",
    613: "Cisco TelePresence Profile 52 (C60)",
    614: "Cisco TelePresence Profile 52 Dual (C60)",
    615: "Cisco TelePresence Profile 65 (C60)",
    616: "Cisco TelePresence Profile 65 Dual (C90)",
    617: "Cisco TelePresence MX200",
    619: "Cisco TelePresence TX9000",
    620: "Cisco TelePresence TX9200",
    621: "Cisco 7821",
    622: "Cisco 7841",
    623: "Cisco 7861",
    626: "Cisco TelePresence SX20",
    627: "Cisco TelePresence MX300",
    628: "IMS-integrated Mobile (Basic)",
    631: "Third-party AS-SIP Endpoint",
    632: "Cisco Cius SP",
    633: "Cisco TelePresence Profile 42 (C40)",
    634: "Cisco VXC 6215",
    635: "CTI Remote Device",
    640: "Usage Profile",
    642: "Carrier-integrated Mobile",
    645: "Universal Device Template",
    647: "Cisco DX650",
    648: "Cisco Unified Communications for RTX",
    652: "Cisco Jabber for Tablet",
    659: "Cisco 8831",
    681: "Cisco ATA 190",
    682: "Cisco TelePresence SX10",
    683: "Cisco 8841",
    684: "Cisco 8851",
    685: "Cisco 8861",
    688: "Cisco TelePresence SX80",
    689: "Cisco TelePresence MX200 G2",
    690: "Cisco TelePresence MX300 G2",
    20000: "Cisco 7905",
    30002: "Cisco 7920",
    30006: "Cisco 7970",
    30007: "Cisco 7912",
    30008: "Cisco 7902",
    30016: "Cisco IP Communicator",
    30018: "Cisco 7961",
    30019: "Cisco 7936",
    30027: "Analog Phone",
    30028: "ISDN BRI Phone",
    30032: "SCCP gateway virtual phone",
    30035: "IP-STE",
    36041: "Cisco TelePresence Conductor",
    36042: "Cisco DX80",
    36043: "Cisco DX70",
    36049: "BEKEM 36-Button Line Expansion Module",
    36207: "Cisco TelePresence MX700",
    36208: "Cisco TelePresence MX800",
    36210: "Cisco TelePresence IX5000",
    36213: "Cisco 7811",
    36216: "Cisco 8821",
    36217: "Cisco 8811",
    36219: "Interactive Voice Response",
    36224: "Cisco 8845",
    36225: "Cisco 8865",
    36227: "Cisco TelePresence MX800 Dual",
    36232: "Cisco 8851NR",
    36235: "Cisco Spark Remote Device",
    36239: "Cisco Webex DX80",
    36241: "Cisco TelePresence DX70",
    36247: "Cisco 7832",
    36248: "Cisco 8865NR",
    36250: "Cisco Meeting Server",
    36251: "Cisco Webex Room Kit",
    36254: "Cisco Webex Room 55",
    36255: "Cisco Webex Room Kit Plus",
    36256: "CP-8800-Video 28-Button Key Expansion Module",
    36257: "CP-8800-Audio 28-Button Key Expansion Module",
    36258: "Cisco 8832",
    36259: "Cisco Webex Room 70 Single",
    36260: "Cisco 8832NR",
    36262: "Cisco ATA 191",
    36263: "Cisco Collaboration Mobile Convergence",
    36265: "Cisco Webex Room 70 Dual",
    36292: "Cisco Webex Room Kit Pro",
    36295: "Cisco Webex Room 55 Dual",
    36296: "Cisco Webex Room 70 Single G2",
    36297: "Cisco Webex Room 70 Dual G2",
    36298: "SIP Station",
    36299: "Cisco Webex Room Kit Mini",
    36302: "Cisco Webex VDI Svc Framework",
    36304: "Cisco Webex Board 55",
    36305: "Cisco Webex Board 70",
    36306: "Cisco Webex Board 85",
    36307: "Cisco Webex Desk Pro",
    36308: "Cisco Webex Room Panorama",
    36309: "Cisco Webex Room 70 Panorama",
    36312: "Cisco Webex Room Phone",
    36319: "Cisco 860",
    36320: "Cisco 840",
    36322: "Cisco Webex Desk LE",
    36324: "Cisco Webex Desk",
    36326: "Cisco Webex Desk Mini",
    36327: "Cisco Webex Desk Hub",
    36333: "Cisco Webex Board Pro 55",
    36334: "Cisco Webex Board Pro 75",
    36335: "Cisco Webex Room Bar",
    36336: "Cisco 8875",
    36337: "Cisco 8875NR",
    36338: "Cisco 8851NS",
    36339: "Cisco 8811NS",
    36340: "Cisco 8841NS",
    36341: "Cisco Room Kit EQ",
    36343: "Cisco Room Bar Pro",
    36344: "Cisco Room Kit EQX",
}


def modelname(modelnum=0):
    return MODEL_NAMES.get(modelnum, modelnum)


# Download phone html page
def gethtml(url):
    try:
        r = requests.get(url, timeout=6)
        r.encoding = "utf-8"
        return r.text
    except Exception:
        return "No connect"


# Insert serial number into phone dict - hits the phone's own embedded web
# page directly over plain HTTP (port 80, no auth), not CUCM, since CUCM
# doesn't expose serial numbers over RisPort70/AXL at all.
def extract_sn(ip, model, num, desc):
    out = {"ip": ip, "model": model, "num": num, "desc": desc}
    if model in ("Cisco 7811", "Cisco 7861"):
        weburl = "http://{}/CGI/Java/Serviceability".format(ip)
        sn_regex = r"</TD><TD><B>([A-Z]{3}\w{8})</B></TD>"
    elif model == "Cisco 3905":
        weburl = "http://{}/Device_Information.html".format(ip)
        sn_regex = r"<td><p><b>([A-Z]{3}\w{8})</b></p></td>"
    elif model in ("Cisco 6961", "Cisco 6921"):
        weburl = "http://{}".format(ip)
        sn_regex = r"</TD><TD><strong>([A-Z]{3}\w{8})</strong></TD>"
    else:
        weburl = "http://{}".format(ip)
        sn_regex = r"</TD><TD><B>([A-Z]{3}\w{8})</B></TD>"

    if (
        model == "Cisco Jabber"
        or model == "Third-party SIP Device"
        or "Webex" in str(model)
        or "Dual Mode" in str(model)
        or "TelePresence" in str(model)
    ):
        sn = "Not supported"
    else:
        html = gethtml(weburl)
        try:
            sn = re.search(sn_regex, html)[1]
        except Exception:
            sn = "No SN found"
    out["sn"] = sn
    return out


def _devices_with_sn(devices: list[dict]) -> list[dict]:
    devices = sorted(devices, key=lambda k: k["ip"])
    with ThreadPoolExecutor(max_workers=30) as executor:
        result = executor.map(
            extract_sn,
            (d["ip"] for d in devices),
            (d["model"] for d in devices),
            (d["num"] for d in devices),
            (d["desc"] for d in devices),
        )
    return list(result)


def scan_phones(num="", name="", ip="", model=255, max=1500) -> list[dict]:
    devices = cucm_rt_phones(model=model, num=num, name=name, ip=ip, max=max)
    return _devices_with_sn(devices)


# CUCM caps a single RisPort70 call to ~1000 registered devices - a site
# with more active phones than that would otherwise silently only get the
# first ~1000 back, with no error to say so. This runs one query per
# configured number-mask prefix instead of one unbounded query, merging
# results by IP (so a phone that happens to match more than one prefix -
# shouldn't normally happen with a sane prefix list, but a typo could
# overlap two of them - is only counted once).
def scan_phones_autosplit(name="", ip="", model=255, max=1000, prefixes=None) -> list[dict]:
    if prefixes is None:
        prefixes = load_scan_prefixes()
    devices_by_ip = {}
    for prefix in prefixes:
        for device in cucm_rt_phones(model=model, num=prefix, name=name, ip=ip, max=max):
            devices_by_ip[device["ip"]] = device
    return _devices_with_sn(list(devices_by_ip.values()))
