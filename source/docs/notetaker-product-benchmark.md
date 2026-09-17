# Local-first notetaker product benchmark

Research snapshot: 2026-09-16.

## Decision

Snipvoice should become an offline meeting-memory workspace: reliable local
capture, structured meeting reports, searchable conversations, cited local Q&A,
and reviewable follow-up artifacts. It should not reproduce the cloud
collaboration, behavioral scoring, or revenue-prediction surface of enterprise
notetakers.

The commercial products below demonstrate that the value above transcription
comes from reusable structure, retrieval, and workflow distribution. Snipvoice's
differentiator is that recording, transcription, storage, retrieval, and summary
inference can remain on the user's machine after explicitly installing models.

## Method and evidence limits

This comparison uses first-party product pages, help centers, privacy pages, and
the Superhuman acquisition announcement. Feature and performance statements are
vendor claims unless otherwise stated; no competitor was installed or tested.
The comparison describes product direction, not independent accuracy, security,
or usability certification.

Prices, plans, integrations, and cloud-processing arrangements can change. Check
the linked sources again before using them in public marketing or a purchasing
decision.

## Benchmark products

### Granola

Granola is the closest product-shape benchmark: a desktop and mobile AI notepad
that records without joining the call as a bot. It works across meeting apps,
combines user-written notes with transcription, and keeps notes private until
the user shares them. Its broader product includes:

- calendar-aware meeting preparation;
- editable AI-enhanced notes;
- custom note templates for meeting types;
- reusable prompt recipes;
- spaces and folders, including recurring-meeting organization;
- chat over one meeting, selected meetings, or folders; and
- Slack, Notion, CRM, Zapier, API, and MCP integrations.

Granola captures from the user's computer and says it does not retain meeting
audio, but its transcription and summaries use external providers such as
Deepgram, AssemblyAI, OpenAI, and Anthropic. Its notes are therefore private by
default, not local-only.

Sources: [product overview](https://www.granola.ai/),
[templates](https://docs.granola.ai/help-center/taking-notes/customise-notes-with-templates),
[recipes](https://docs.granola.ai/help-center/getting-more-from-your-notes/recipes),
[spaces and folders](https://docs.granola.ai/help-center/sharing/folders/spaces-and-folders),
and [security](https://www.granola.ai/security).

**Relevant lesson:** adopt templates, recipes, organization, and meeting-memory
workflows while retaining local inference and explicit sharing.

### Fathom

Fathom supports calendar-driven capture for Zoom, Google Meet, and Microsoft
Teams, with visible bot and bot-free modes. Its product surface includes:

- live transcription and summaries;
- highlights, bookmarks, clips, and recordings;
- multiple and customizable summary templates;
- action-item extraction with owners;
- follow-up email drafts;
- question answering over calls and deals;
- global and AI-assisted search; and
- CRM synchronization, Slack, Zapier, API, and webhooks.

Fathom stores its service data in the United States. It says third-party AI
providers may not train on customer data, while de-identified customer data may
be used to improve Fathom's proprietary models unless the applicable setting is
disabled. Enterprise plans support organization-wide retention policies.

Sources: [product overview](https://fathom.video/alt),
[advanced AI features](https://help.fathom.video/en/articles/640768),
[settings and capture behavior](https://help.fathom.video/en/articles/3239617),
[security](https://help.fathom.video/en/articles/296512), and
[retention policies](https://help.fathom.video/en/articles/6057089).

Superhuman's acquisition announcement says Fathom adds meeting capture and
meeting intelligence to its email, calendar, document, database, and agent
surfaces. This supports the strategic conclusion that meeting context becomes
more valuable when it can feed later work.

Source: [Superhuman acquisition announcement](https://blog.superhuman.com/superhuman-acquires-fathom/).

**Relevant lesson:** adopt highlights, report profiles, follow-up drafts, and
transparent consent. Do not make calendar auto-join or CRM access a core
dependency.

### Read AI

Read AI expands beyond meetings into email, messaging, enterprise search, and
connected work systems. Its product includes:

- meeting summaries, action items, key questions, and full transcripts;
- audio/video playback with automatically highlighted moments;
- meeting-assistant, browser, desktop, mobile, and in-person capture;
- cross-source Q&A over meetings, email, messages, documents, CRM, and project
  systems;
- scheduling and distribution automation; and
- sentiment, engagement, talk-time, and coaching metrics.

Read is a cloud service. Its privacy policy describes collecting meeting audio,
video, metadata, and connected-service content. Its documentation says most
processing is internal while a smaller conversational layer uses external AI
providers under contractual retention and reuse restrictions.

Sources: [feature overview](https://support.read.ai/hc/en-us/articles/23272882203923-How-do-I-get-started-with-Read),
[desktop application](https://support.read.ai/hc/en-us/articles/45911611006995-How-to-Use-Read-s-Desktop-App-for-Windows-and-Mac),
[Ask Read](https://support.read.ai/hc/en-us/articles/40878644929427-FAQ-Ask-Read-with-LLMs),
and [privacy policy](https://www.read.ai/privacy-policy).

**Relevant lesson:** key-question extraction and cited knowledge search are
useful. Sentiment, engagement, and employee-coaching scores create accuracy,
privacy, and workplace-surveillance risks without strengthening Snipvoice's
local core.

### Otter.ai

Otter combines mature transcription with a broader meeting-agent product. Its
current surface includes:

- bot-based and bot-free desktop capture;
- live transcription, speaker recognition, and synchronized playback;
- summaries, decisions, action items, and follow-up content;
- search across meetings and connected applications;
- chat over one or many meetings;
- custom vocabulary, folders, comments, and sharing; and
- Slack, CRM, storage, API, webhook, and MCP integrations.

Otter is cloud-based. Its privacy materials describe cloud storage and service
providers, and its current privacy policy permits improvement of proprietary AI
technology using de-identified audio and transcripts under the stated legal
bases and controls.

Sources: [product overview](https://otter.ai/),
[meeting-agent announcement](https://otter.ai/blog/otter-meeting-agent-your-new-collaborative-teammate),
[privacy and security](https://otter.ai/privacy-security), and
[privacy policy](https://otter.ai/privacy-policy).

**Relevant lesson:** add speaker-aware transcript navigation, vocabulary, and
local chat. Snipvoice can offer stronger defaults around local retention and
data movement.

### Elephan.AI

Elephan positions meeting intelligence inside a Brazilian revenue-intelligence
platform. It combines meetings with calls, WhatsApp, email, CRM, and customer
success systems. Its advertised surface includes:

- recording and transcription;
- a unified interaction timeline;
- keywords, sentiment, objections, and competitor monitoring;
- seller and meeting-type scorecards;
- automatic CRM population;
- conversational analysis across operational data;
- task creation and execution; and
- deal-risk, prioritization, and forecast signals.

These are vendor-described capabilities. Public materials establish a managed
platform and integrations, not an offline or local-only processing mode.

Sources: [platform overview](https://www.elephan.ai/plataforma),
[integrations](https://www.elephan.ai/integracoes), and
[terms](https://www.elephan.ai/termos-e-condicoes-de-uso).

**Relevant lesson:** structured extraction of risks, objections, commitments,
and next steps is useful. Revenue prediction, lead enrichment, behavioral
scorecards, and automatic CRM mutation are outside Snipvoice's initial scope.

## Current Snipvoice baseline

Snipvoice already implements much of the difficult local foundation:

- independent microphone and selected system-output capture on Windows and
  macOS;
- append-only segmented storage, explicit gaps, pause/resume, and interrupted
  session recovery;
- local meeting search, notes, bookmarks, playback, and transcript revisions;
- bounded audio import and text, JSON, Markdown, mixed-WAV, and per-track
  exports;
- installed-only meeting transcription;
- in-process llama.cpp summaries using explicitly installed, hash-verified
  models; and
- cited summaries, decisions, and action items with unknown owners and
  deadlines preserved as unknown.

See the [README](../../README.md#highlights), the
[meeting implementation plan](offline-meeting-implementation-plan.md), and the
[summary contract](../meeting_summary.py).

Current boundaries remain important:

- source labels distinguish microphone from system audio, not individual
  speakers;
- diarization and acoustic echo cancellation are not implemented;
- summary output uses one fixed schema rather than user-selectable report
  profiles;
- search is ordinary local text search rather than semantic retrieval;
- cross-meeting Q&A, calendar integration, cloud connectors, and automatic
  meeting detection were deliberately deferred; and
- packaged real-device capture, long-session drift, device switching, and
  offline operation still require physical acceptance on supported platforms.

## Recommended roadmap

### 0. Close capture acceptance before expanding the intelligence layer

Complete the outstanding physical Windows and macOS validation for:

- microphone-only, output-only, and simultaneous capture;
- default and manually pinned devices;
- two-hour memory, disk, synchronization, and drift behavior;
- endpoint changes, unplug/reconnect, permissions, pause, and one-source loss;
- packaged cold startup and processing with networking disabled; and
- recovery from interruption, disk failure, and slower-than-realtime ASR.

The new product layer depends on trustworthy recordings. Unit and hosted build
success do not replace physical capture evidence.

### 1. Add report profiles and reusable recipes

Provide built-in profiles such as General, One-on-one, Interview, Sales,
Customer Feedback, Project Update, and Retrospective. Let the user create local
custom profiles without arbitrary code execution.

A report profile may request a subset of:

- concise summary;
- decisions;
- action items with owner and deadline;
- open questions;
- risks and objections;
- customer or product feedback; and
- a follow-up email draft.

Every factual item must cite transcript segment IDs. Missing people, owners,
deadlines, or decisions remain null or explicitly unknown. Generated reports are
editable revisions and never overwrite manual notes.

**Acceptance:** deterministic schema validation, bounded prompts and output,
preserved citations, preserved earlier revisions, offline inference, and no
automatic external side effects.

### 2. Improve transcript interaction and highlights

Add synchronized transcript-to-playback navigation, manual speaker naming,
search-result jumps, and bookmark-to-highlight conversion. Permit explicit
export of a selected text excerpt or short audio clip without changing the raw
recording.

Manual speaker labels should come before automatic diarization. They are useful
with the current transcript and do not require another model, license, runtime,
or accuracy claim.

**Acceptance:** edits and labels survive reprocessing; exports cite source
session and time range; raw tracks remain unchanged; long transcripts load and
navigate with bounded memory.

### 3. Add local question answering

Implement `Ask this meeting` before cross-meeting Q&A. Answers must use only the
selected meeting and cite the transcript segments supporting each claim.

After the single-meeting path is reliable, add cross-meeting retrieval in two
stages:

1. SQLite full-text retrieval over titles, notes, transcripts, and reviewed
   reports.
2. Optional local embeddings only after measuring model size, multilingual
   quality, retrieval accuracy, latency, and memory on supported hardware.

Cross-meeting answers must identify both session IDs and transcript segments.
No answer should silently treat generated summaries as primary evidence when a
transcript is available.

**Acceptance:** offline operation, reproducible source citations, bounded index
growth, deletion propagation, revision handling, and useful Portuguese and
English retrieval tests.

### 4. Add local organization and privacy controls

Add local folders, tags, people or project labels, and recurring-meeting groups
without requiring calendar access. A meeting may belong to multiple folders.

Add opt-in retention rules for meetings and raw audio. Useful choices include:

- keep everything until manual deletion;
- delete raw source audio after a reviewed transcript exists;
- retain audio for a user-selected period; and
- retain the reviewed transcript and report while deleting derived audio.

Destructive rules must be previewable, explicit, recoverable where practical,
and disabled by default. The UI must not claim forensic secure erasure on SSDs.

Recording state should remain continuously visible. Add a concise consent
reminder or copyable consent notice without integrating into meeting chat.

### 5. Expose a controlled local interface

After local Q&A and access boundaries are stable, consider an opt-in read-only
MCP server or local API. It should:

- bind only to loopback by default;
- require authentication even on loopback;
- expose only meetings or folders explicitly selected by the user;
- return source citations and reviewed artifacts distinctly;
- log access locally without recording transcript content in logs; and
- provide no mutation, sending, CRM, calendar, or task-execution tools in the
  first version.

This supplies the distribution benefit demonstrated by Granola, Fathom, Otter,
and Superhuman without requiring Snipvoice to ingest the user's entire cloud
workspace.

### 6. Evaluate local diarization separately

Automatic speaker diarization is valuable but should be an experimental,
separately accepted capability. Select a candidate only after checking license,
model size, runtime packaging, Portuguese behavior, overlapping speech, room
microphones, CPU/GPU requirements, and correction UX.

Do not convert microphone/system track labels into speaker claims. A system
track can contain several remote speakers, and a room microphone can contain
several local speakers.

## Explicit non-goals for the local-first core

- Meeting bots and automatic calendar joins.
- Implicit calendar, email, browser, CRM, Slack, or WhatsApp ingestion.
- Cloud-hosted sharing links and multi-tenant team workspaces.
- Automatic email sending, CRM writes, task creation, or scheduling.
- Sentiment, engagement, employee-performance, or coaching scores.
- Predictive pipeline, deal, lead, or revenue scoring.
- Always-on or hidden recording.
- Silent model downloads or cloud fallbacks.

These features may be reconsidered only as explicit, opt-in connectors with a
separate privacy and authorization model. They are not prerequisites for a
useful private meeting-memory product.

## Product position

Snipvoice's strongest position is:

> Private dictation and meeting memory that remains usable with the network
> disconnected.

The near-term product should deliver Granola/Fathom-style ergonomics over a
local transcript-and-summary vault. Its value comes from reliable capture,
traceable structured output, and controlled reuse of meeting knowledge—not from
collecting the rest of the user's organization into another cloud platform.
