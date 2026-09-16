# Local meeting-memory glossary

- **Meeting bundle** — one app-owned directory containing a recording and all
  durable material derived from it.
- **Transcript revision** — immutable ASR output for one profile/language run.
- **Annotation** — human-owned information layered over a meeting or transcript
  revision without changing generated source data.
- **Report profile** — a reusable selection of supported report sections plus
  bounded instructions.
- **Report revision** — one generated structured artifact bound to a transcript
  revision, profile version, and exact model artifact.
- **Reviewed artifact** — user-edited output derived from a report revision; it
  never overwrites the generated payload.
- **Collection** — a user-created folder or project grouping. Tags are
  lightweight labels, and a series is a manually assigned recurring-meeting
  group.
- **Catalog index** — the disposable SQLite projection used for listing,
  filtering, snippets, and retrieval.
- **Retention plan** — an immutable preview of exact app-owned targets and lost
  capabilities produced before a destructive operation.
