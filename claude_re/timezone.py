"""System IANA timezone detection."""
import datetime
import sys


def detect_timezone():
    """Detect system IANA timezone. Falls back to UTC with warning."""
    # Method 1: Python 3.9+ datetime.astimezone()
    try:
        tz = datetime.datetime.now().astimezone().tzinfo
        if hasattr(tz, 'key'):
            return tz.key
        name = tz.tzname(datetime.datetime.now())
        if name and '/' in name:
            return name
    except Exception:
        pass

    # Method 2: try tzlocal (if installed)
    try:
        from tzlocal import get_localzone
        tz = str(get_localzone())
        if tz and '/' in tz:
            return tz
    except ImportError:
        pass

    # Method 3: Windows registry
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation")
            tz_name, _ = winreg.QueryValueEx(key, "TimeZoneKeyName")
            winreg.CloseKey(key)
            _win_to_iana = {
                "India Standard Time": "Asia/Kolkata",
                "Eastern Standard Time": "America/New_York",
                "Pacific Standard Time": "America/Los_Angeles",
                "Central Standard Time": "America/Chicago",
                "Mountain Standard Time": "America/Denver",
                "GMT Standard Time": "Europe/London",
                "Central European Standard Time": "Europe/Berlin",
                "Tokyo Standard Time": "Asia/Tokyo",
                "China Standard Time": "Asia/Shanghai",
                "AUS Eastern Standard Time": "Australia/Sydney",
            }
            if tz_name in _win_to_iana:
                return _win_to_iana[tz_name]
        except Exception:
            pass

    # Method 4: /etc/timezone (Linux)
    try:
        with open("/etc/timezone") as f:
            tz = f.read().strip()
            if tz and '/' in tz:
                return tz
    except Exception:
        pass

    print("[!] WARNING: Could not detect system timezone. Falling back to UTC.")
    return "Etc/UTC"
