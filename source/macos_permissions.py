"""macOS TCC gates for the keyboard listener and the synthesized paste.

Two independent grants stand between the app and a working expansion on macOS:

- **Input Monitoring** (``kTCCServiceListenEvent``) for the global
  ``pynput`` listener that reads the trigger.
- **Accessibility** (``kTCCServiceAccessibility``) for the ``pynput``
  ``Controller`` that sends Cmd+V.

Neither failure is visible from Python. pynput's darwin backend does *not*
raise when untrusted: ``Listener.start()`` returns, the thread stays alive and
``on_press`` simply never fires, with a single line on **stderr** that
``pythonw`` and the packaged build throw away (field notes in issue #24). So
the state has to be probed explicitly, before anything relies on it:

- ``IOHIDCheckAccess(kIOHIDRequestTypeListenEvent)`` for Input Monitoring.
- ``AXIsProcessTrusted()`` for Accessibility.

Both are read-only — neither prompts, neither adds the app to a pane — which is
why the flow ends in a dialog that deep-links the panes instead.

Everything here is inert off macOS: the checks answer ``unknown`` and the
decision layer then asks for nothing. The framework symbols are resolved lazily
so importing this module on Windows or Linux costs nothing and cannot fail.
"""

import ctypes
import ctypes.util
import subprocess

from platform_support import IS_MAC


INPUT_MONITORING = "input_monitoring"
ACCESSIBILITY = "accessibility"
MICROPHONE = "microphone"

# Order matters: it is the order the user is asked to fix them, and Input
# Monitoring comes first because without it nothing is ever detected.
# Microphone is intentionally not in this tuple: it is a voice-only grant and
# must not trigger expansion onboarding.
PERMISSIONS = (INPUT_MONITORING, ACCESSIBILITY)

GRANTED = "granted"
DENIED = "denied"
UNKNOWN = "unknown"

# The pane deep-links. pynput's own stderr warning says "accessibility
# clients", inherited from the pre-Catalina API, but the global listener is
# gated by Input Monitoring on modern macOS — sending the user to the pane the
# warning names would have them grant the wrong thing.
SETTINGS_PANE_URLS = {
    INPUT_MONITORING: "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
    ACCESSIBILITY: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    MICROPHONE: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
}

PERMISSION_LABELS = {
    INPUT_MONITORING: "Monitoramento de Entrada",
    ACCESSIBILITY: "Acessibilidade",
    MICROPHONE: "Microfone",
}

PERMISSION_REASONS = {
    INPUT_MONITORING: "detectar o atalho digitado",
    ACCESSIBILITY: "colar o texto expandido",
    MICROPHONE: "gravar o ditado",
}

# IOKit/hid/IOHIDLib.h: IOHIDRequestType and IOHIDAccessType.
_REQUEST_TYPE_LISTEN_EVENT = 0
_ACCESS_TYPE_BY_CODE = {0: GRANTED, 1: DENIED, 2: UNKNOWN}

# Resolved framework symbols, keyed by ``(framework, symbol)``. Only successful
# resolutions are cached: ``secure_input_enabled`` runs per detected trigger on
# the keyboard listener thread, and ``find_library`` + ``LoadLibrary`` per call
# is real cost there, but a failed resolution must stay retryable (the caller's
# documented answer to a missing symbol is "not secure / unknown", never a
# permanent one). Tests that exercise ``_framework_symbol`` directly clear it.
_SYMBOL_CACHE = {}


def _framework_symbol(framework, symbol):
    """Return a callable for ``symbol`` in ``framework``, or None if unavailable.

    Resolution failures are a legitimate answer here (an older macOS without
    ``IOHIDCheckAccess``, a stripped runtime): the caller reports ``unknown``
    and the app stays quiet rather than nagging about a state it cannot read.

    A successful lookup is memoized so the per-keystroke secure-input probe does
    not re-run ``find_library``/``LoadLibrary`` every time; the live C call the
    callers make on the returned symbol is never cached, so the probe stays
    fresh. Failures are not cached, preserving retry.
    """
    key = (framework, symbol)
    cached = _SYMBOL_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        path = ctypes.util.find_library(framework)
        if not path:
            return None
        resolved = getattr(ctypes.cdll.LoadLibrary(path), symbol, None)
    except (OSError, AttributeError):
        return None
    if resolved is not None:
        _SYMBOL_CACHE[key] = resolved
    return resolved


def check_input_monitoring():
    """Return the Input Monitoring grant state for this process."""
    if not IS_MAC:
        return UNKNOWN

    check_access = _framework_symbol("IOKit", "IOHIDCheckAccess")
    if check_access is None:
        return UNKNOWN
    try:
        check_access.restype = ctypes.c_int
        check_access.argtypes = [ctypes.c_uint32]
        return _ACCESS_TYPE_BY_CODE.get(check_access(_REQUEST_TYPE_LISTEN_EVENT), UNKNOWN)
    except OSError:
        return UNKNOWN


def check_accessibility():
    """Return the Accessibility grant state for this process."""
    if not IS_MAC:
        return UNKNOWN

    is_trusted = _framework_symbol("ApplicationServices", "AXIsProcessTrusted")
    if is_trusted is None:
        return UNKNOWN
    try:
        is_trusted.restype = ctypes.c_bool
        is_trusted.argtypes = []
        # AXIsProcessTrusted has no "unknown": untrusted covers both a denial
        # and a fresh account that was never asked.
        return GRANTED if is_trusted() else DENIED
    except OSError:
        return UNKNOWN


def secure_input_enabled():
    """True when macOS Secure Keyboard Entry is active for the focused app.

    This is a separate gate from TCC and it cannot be granted: while it is on
    (Terminal's "Secure Keyboard Entry", a focused password field, the login
    window) the system drops *both* directions of an expansion — the listener
    never sees the trigger and the synthesized backspaces and Cmd+V never land.

    The app checks this before erasing anything, so a trigger typed into a
    secure field is left exactly as typed instead of being half-erased by
    backspaces that some other app might receive. Always False off macOS.
    """
    if not IS_MAC:
        return False

    is_enabled = _framework_symbol("Carbon", "IsSecureEventInputEnabled")
    if is_enabled is None:
        return False
    try:
        is_enabled.restype = ctypes.c_bool
        is_enabled.argtypes = []
        return bool(is_enabled())
    except OSError:
        return False


SECURE_INPUT_MESSAGE = (
    "Entrada segura do macOS ativa (campo de senha ou Terminal com "
    "\"Secure Keyboard Entry\"). O snippet não foi expandido."
)


def check_microphone():
    """Return the microphone grant state. Voice-only; unused by expansion.

    Off macOS this answers ``granted``: Windows and Linux surface denial when
    the capture device fails to open. A missing AVFoundation symbol on macOS
    answers ``unknown`` and must not nag.
    """
    if not IS_MAC:
        return GRANTED
    try:
        import AVFoundation
    except Exception:
        return UNKNOWN
    try:
        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
            AVFoundation.AVMediaTypeAudio
        )
    except Exception:
        return UNKNOWN
    # 0 notDetermined, 1 restricted, 2 denied, 3 authorized
    if int(status) == 3:
        return GRANTED
    if int(status) in (1, 2):
        return DENIED
    return UNKNOWN


def check_permissions():
    """Probe both grants. Returns ``{permission: granted|denied|unknown}``."""
    return {
        INPUT_MONITORING: check_input_monitoring(),
        ACCESSIBILITY: check_accessibility(),
    }


# ---------------------------------------------------------------------------
# Decision layer (pure; this is what the tests drive)
# ---------------------------------------------------------------------------

def denied_permissions(status):
    """Return the denied permissions, in the order the user should fix them."""
    return [name for name in PERMISSIONS if status.get(name) == DENIED]


def unknown_permissions(status):
    """Return the permissions whose state could not be read."""
    return [name for name in PERMISSIONS if status.get(name, UNKNOWN) == UNKNOWN]


def needs_onboarding(status):
    """True when the user must be shown the permission dialog.

    Only an explicit denial asks for anything. ``unknown`` means the probe
    itself failed, and a dialog demanding a grant the app cannot verify would
    be unfalsifiable — it gets logged instead.
    """
    return bool(denied_permissions(status))


def describe_status(status):
    """One-line ``AppLogger`` rendering of a probe result."""
    return ", ".join(
        f"{PERMISSION_LABELS[name]}={status.get(name, UNKNOWN)}" for name in PERMISSIONS
    )


def build_prompt_message(status):
    """PT-BR body for the onboarding dialog, listing only what is missing."""
    missing = denied_permissions(status)
    if not missing:
        return ""

    lines = [
        "O Snipvoice precisa de permissões do macOS para funcionar.",
        "",
    ]
    for name in missing:
        lines.append(f"• {PERMISSION_LABELS[name]} — para {PERMISSION_REASONS[name]}.")
    lines += [
        "",
        "Sem elas o app abre normalmente, mas nada é expandido:",
        "o macOS bloqueia a captura do atalho em silêncio.",
        "",
        "O Snipvoice não armazena nem envia o que você digita;",
        "todo o processamento acontece no seu Mac.",
        "",
        "Abra o painel, marque o Snipvoice na lista e reinicie o app.",
    ]
    return "\n".join(lines)


def build_tray_message(status):
    """Short PT-BR tray/notification line for a denied state."""
    missing = denied_permissions(status)
    if not missing:
        return ""
    names = " e ".join(PERMISSION_LABELS[name] for name in missing)
    return f"Permissão do macOS pendente: {names}. A expansão não vai funcionar."


RECHECK_RESOLVED = "resolved"
RECHECK_PARTIAL = "partial"
RECHECK_PENDING = "pending"


def recheck_outcome(previous, current):
    """Classify a re-check against the state the dialog was opened with.

    Returns ``(state, message)``. A grant is never reported as "working now":
    TCC decisions are read by the frameworks at process start, so a listener
    that was already refused stays dead for this process's lifetime. The honest
    answer is to ask for a restart — see issue #25, item 4.

    Only an explicit ``granted`` clears a permission. One that answers
    ``unknown`` on the re-check is still reported as missing: the probe failing
    is not the user having fixed it.
    """
    was_denied = denied_permissions(previous)
    resolved = {name for name in was_denied if current.get(name) == GRANTED}
    still_denied = [
        name
        for name in PERMISSIONS
        if (name in was_denied and name not in resolved) or current.get(name) == DENIED
    ]

    if not still_denied:
        return RECHECK_RESOLVED, (
            "Permissões concedidas. Reinicie o Snipvoice para que a captura "
            "de teclado passe a funcionar."
        )

    names = " e ".join(PERMISSION_LABELS[name] for name in still_denied)
    if len(still_denied) < len(was_denied):
        return RECHECK_PARTIAL, (
            f"Ainda falta: {names}. Conceda a permissão restante e reinicie o "
        "Snipvoice."
        )
    return RECHECK_PENDING, (
        f"Nada mudou: {names} continua sem permissão. Marque o Snipvoice na "
        "lista do painel do macOS."
    )


def open_settings_pane(permission, runner=None):
    """Deep-link System Settings to ``permission``'s pane. True when launched.

    ``open`` rather than ``webbrowser``: the ``x-apple.systempreferences:``
    scheme is handled by LaunchServices, and routing it through a browser is an
    extra hop that can land on a "no application" error instead of the pane.
    """
    url = SETTINGS_PANE_URLS.get(permission)
    if not url:
        return False

    run = runner if runner is not None else subprocess.run
    try:
        result = run(["open", url], capture_output=True, text=True)
    except (OSError, ValueError):
        return False
    return getattr(result, "returncode", 1) == 0
