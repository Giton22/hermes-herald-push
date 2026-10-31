# herald-push

A Hermes plugin that sends your bots' messages to the [Kyllene](https://github.com/Giton22/kyllene) Android app
even when the phone can't reach the gateway (off the tailnet, on mobile data). Messages travel through
[ntfy](https://ntfy.sh), are **end-to-end encrypted** to the phone, and are **signed** by the gateway. ntfy
only ever sees opaque envelopes on unguessable topics. Google services are not involved.

## Install

From Kyllene: **Settings → Notifications anywhere**. Kyllene installs this plugin on the gateway it's signed in
to and registers the phone, in one step.

Or by hand on the gateway:

```
hermes plugins install https://github.com/Giton22/hermes-herald-push
```

## How it works

- **Registration.** The phone registers over the dashboard's authenticated connection with
  `/herald-push register`. The command refuses to run on messaging platforms, so someone chatting with a bot on
  Telegram can't add their own phone. At registration the phone and the gateway swap public keys, and each pins
  the other's.
- **Watching.** One watcher, in the dashboard or the messaging gateway process, reads every profile's
  `state.db` read-only and pushes each new final reply in a bot's Bot Chat.
- **Crypto.** P-256 ECDH to an ephemeral key, HKDF-SHA256, AES-256-GCM bound to the topic, and ECDSA P-256
  over everything, verified before decrypting. The wire format is in Kyllene's
  [`docs/push-protocol.md`](https://github.com/Giton22/kyllene/blob/master/docs/push-protocol.md).
- **Storage.** The gateway identity and the device list are in `<hermes root>/plugin-data/herald-push/`, with
  file mode 0600.

**Not yet:** replying or approving through push. Those still use the direct connection.

## Cron runs (0.4.0)

Finished cron runs for Kyllene's Inbox, for the jobs a phone follows.

- **Read from the output files.** Hermes writes every finished run to `<home>/cron/output/<job>/<time>.md`,
  whatever the job delivers to. The watcher reads those, in every profile, so a job keeps going to Telegram or
  wherever it went before; following it here only adds the phone.
- **Per job, per phone.** Kyllene sends `/herald-push cron follow` with `device_id`, `jobs` (job id to `notify`,
  `quiet` or `off`), `default` (what every job not listed does, `off` unless asked; with `quiet` or `notify` a
  job made later follows too) and `failures` (any failed run notifies, followed or not). `cron following` reads it back. The
  list is in `cron.json` (0600) and is kept per registered phone.
- **What is sent.** A `cron_run` push: `bot` (profile), `job`, `run` (the output file's name), `label` (the
  job's name), `status` (`ok` or `failed`), `notify`, `at` and the start of the output as `text` (about 1,800
  characters, like a bot message). Kyllene fetches the whole run over the direct connection.
- **The Inbox.** `cron inbox` lists the runs a phone follows (and every failure, when it asked), newest first,
  each with the start of its text; `limit` and `before` page back. `cron read` gives one run in full (`bot`,
  `job`, `run`, up to 48 KB; a longer one is cut and says `"truncated": true`, 0.7.2). Both read the same
  output files, so they work for every profile.
- **The run's session (0.7.2).** Runs name the session they ran in, so Kyllene can open it: `session` on the
  `cron_run` push, each `cron inbox` row and the `cron read` reply, from the profile's `state.db` (read-only),
  a compressed session followed to its continuation. From Hermes 0.22.3 an agent job's run keeps the runtime's
  `local-…` id, found through the scheduler's admission of it. A run with no session (a gate that kept the agent asleep,
  or one whose session never ended or was swept up as an orphan) has none: the push leaves the field out, while
  `cron inbox` rows and the `cron read` reply always carry `session`, null when there is none.
- **What isn't.** Runs that stayed silent (`[SILENT]`, a script with no output, a gate that didn't wake the
  agent), a job's history when it is first seen, and runs older than 30 minutes when found (after a restart).

## Phone tools (0.3.3)

Agent tools that run on the phone, in the `herald_phone` toolset: `phone_battery`, `phone_network`,
`phone_sound`, `phone_alarm`, `phone_screen_time`, `phone_open`, `phone_clipboard`, `phone_location`,
`phone_geofence`, `phone_calendar`, `phone_contacts`, `phone_health`, `phone_call_me` (0.5.0) and
`phone_show_ui` (0.6.0, an experiment).

- **Off until the user turns them on.** Each tool has its own switch in Kyllene (Settings → Phone tools). Kyllene
  tells the gateway which ones are on with `/herald-push phone tools`, and a tool no phone has on is not
  offered to the agent at all. Hermes fixes a chat's tools when the chat starts, so a switch changes what new
  chats get; a call from an older chat to a tool that's off now is refused, and so is a call still waiting.
- **Not on messaging platforms.** A turn from Telegram, Discord and the like gets a refusal: others may be
  talking to the bot there.
- **One phone, one key.** The first report binds the phone's id to a secret key (the gateway keeps its
  hash), and every later phone command must carry it.
- **The phone you chat from.** Kyllene tells the gateway which chat connections it sends messages on
  (`/herald-push phone here`), and a call from a turn started there goes to that phone, even when another
  phone has the same tool on. If that phone has the tool off, the call is refused rather than sent to another
  phone. A turn without a chat connection (a side question or background task, which Hermes runs apart from
  the chat) goes to a phone that sent a turn in the last two minutes. Any other turn no phone started (a cron
  job, Desktop, the dashboard) goes to the first phone, in the order the user set in Kyllene, that has the tool
  on; phones not placed follow by last use. Kyllene lists the phones (`/herald-push phone list`), orders them
  (`phone order`) and removes one the user doesn't have any more (`phone remove`); the list shows names and
  switches, never another phone's key or push registration.
- **Changes ask first.** Setting an alarm or Do Not Disturb, opening something, copying, adding or moving an
  event and adding a watched place are shown on the phone, and only happen if the user allows them there.
- **How a call travels.** The tool writes a request file to `<hermes root>/plugin-data/herald-push/phone/`
  (files, because the agent can run in another process than the dashboard). Kyllene learns of it from the
  chat's tool event, a push, or when it connects, reads it with `/herald-push phone pending` and answers with
  `/herald-push phone answer`, both over the dashboard's authenticated connection only. Reads wait 30 seconds
  for the phone, changes 2 minutes; then the agent is told the phone didn't answer.
- **The agent calls you.** `phone_call_me(reason, details?)` rings the user's own phone like an incoming call
  (and Kyllene on their watch). It takes no number: it can only ring a phone that switched it on in Kyllene
  (Settings → Phone tools → Calls). Answering puts Hermes on the line by voice, opening with the reason. The
  tool waits up to 75 seconds (the phone rings 45) and returns `answered`, `declined`, `no_answer`, `busy`
  (already on a call with Hermes), `quiet` (quiet hours on the phone, 0.5.1) or `unreachable`, each with a note; anything but `answered` tells the agent
  to reach the user another way. The push carries the reason and details (end-to-end encrypted, like every
  push), so the phone rings even when Kyllene isn't connected; the answer comes back over the socket. It works
  from scheduled jobs too, for "call me at 7:30".
- **A screen in the chat (experiment).** `phone_show_ui(messages, wait?)` takes [A2UI](https://a2ui.org) v0.9
  messages, and Kyllene on Android draws them under the reply with the A2UI Basic Catalog. With `wait` (the
  default) the tool waits up to 5 minutes and returns the button the user tapped, its context and the form's
  values; messages that aren't A2UI come back as an error the agent can fix. The screen is drawn from the call's
  own arguments, so it stays in the chat's history.

## Tests

```
pip install cryptography httpx pyyaml pytest
pytest
```
