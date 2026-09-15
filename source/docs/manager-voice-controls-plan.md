> Historical Sniptype record, retained unchanged from the predecessor. Release, path, and issue references describe Sniptype. See [extraction.md](extraction.md) for the current Snipvoice boundary.

# Manager Voice Controls

## Outcome

The snippet manager has a dedicated **Entrada por voz** tab whenever the
optional `VoiceController` is available. It mirrors the tray's enable action
and embeds the existing profile, language, shortcut, license, download, and
model-removal controls.

The implementation lives in `source/sniptype.pyw`. It does not change the
voice settings schema, transcription runtime, model catalog, optional
dependencies, packaging, or release channel.

## Shared behavior

- The tray settings shortcut opens the manager and selects the voice tab.
- Enable and disable still route through `toggle_voice`; microphone permission
  and model-download confirmation keep their existing behavior.
- Profile and language options retain the current catalog constraints. In
  particular, the Accuracy profile forces automatic language detection.
- Voice status changes cross the existing `GuiThread` boundary before touching
  either the recording indicator or manager widgets.
- Transient recording, transcribing, and routing states still avoid rebuilding
  the tray menu.
- The tab is absent when optional voice-controller construction fails, leaving
  normal snippet expansion and every other manager tab unchanged.

## Lifecycle constraints

The manager voice refresher is separate from `_manager_refreshers`, which is
reserved for snippet-library replacement. The voice callback is rebound for
each manager window and cleared before that window is destroyed.

The tab's `StringVar` instances are owned by the manager `Toplevel` and released
on the GUI thread during close. This matters on macOS: allowing their finalizers
to reach Tcl later from a tray or worker thread can abort the process.

Voice disable may join capture workers, so the manager and tray callbacks launch
it through `BackgroundTaskRunner`. Controller status callbacks provide the
authoritative final state. Cancelled enable and denied microphone paths refresh
the manager immediately so its checkbox cannot remain stale.

## Regression coverage

`source/tests/test_hotpath.py` covers:

- GUI-thread status refresh without transient tray rebuilds;
- cancelled and permission-denied enable flows;
- missing voice support and late callbacks after manager close; and
- asynchronous disable behavior.

`source/tests/test_manager_gui_smoke.py` covers:

- construction of the embedded controls;
- state and installed-model label refresh;
- tray settings routing to the manager tab;
- suppression when voice support is unavailable; and
- close/reopen callback cleanup and rebinding.

Run the focused checks from `source`:

```powershell
python -m unittest tests.test_manager_gui_smoke tests.test_hotpath.VoiceIsolationTests tests.test_ui_theme tests.test_voice_support tests.test_gui_thread -v
```

Then run the complete suite:

```powershell
python -m unittest discover -s tests -v
```

Live microphone-to-paste and packaged desktop behavior remain physical smoke
tests; this manager surface does not change that release gate.
