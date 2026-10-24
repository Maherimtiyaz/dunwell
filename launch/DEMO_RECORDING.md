# Recording the 40-second demo

The demo is the whole pitch. Nobody reads a landing page; they watch eight
seconds of something moving and decide. So this is written to be shot in one
take, on the first try, without an editor.

Target: **40 seconds, 1080p, no narration required.** Silence plus captions
beats a bad voiceover, and captions survive being watched on mute in a
subreddit feed.

---

## 1. Pre-flight (5 minutes, do this once)

### Seed an account

The dashboard needs a signed-in account before it shows anything. Create it
off-camera so the recording starts on the product, not a signup form.

```powershell
cd D:\dunwell
python -m dunwell.serve --port 8906
```

Open <http://127.0.0.1:8906/dashboard> and:

1. **Create account** with something plain - `demo@northwind.example` /
   a throwaway password. Nothing here should look like a real customer.
2. Sign in, but **do not** click **Load demo** yet. That click is the first
   thing you record.

Leave the tab open and signed in.

### Clean the frame

| Thing | Why |
| --- | --- |
| Close every other window | Game Bar captures one window, but a stray popup ruins a take |
| `Win+A` -> Focus assist **On** | Kills notification toasts mid-recording |
| Browser full screen (`F11`) | Removes tabs and the address bar |
| Zoom 100% (`Ctrl+0`) | The layout is designed for it |
| A fresh browser profile | No bookmarks bar, no extension icons, no "restore pages?" bubble |
| Close Slack, Discord, mail | They pop over full-screen windows |

Use **`127.0.0.1`**, not the public URL. Free-tier PythonAnywhere is one
worker and slow; a four-second spinner on camera kills the take.

### Game Bar settings

`Win+G` to open the overlay, then the gear icon:

- **Capturing** -> *Record the last* does not matter here, but set
  **Video quality** to *High* and **Frame rate** to *30 fps*.
- **Audio** -> turn **microphone off** unless you are actually narrating.
  System audio stays on so the clicks are audible, which makes it feel real.
- **Max recording length** -> leave the default; 30 minutes is plenty.

Captures land in `C:\Users\<you>\Videos\Captures`.

---

## 2. The shot list

`Win+Alt+R` starts and stops. Nothing else.

| Time | What is on screen | What you do | Caption |
| --- | --- | --- | --- |
| 0:00-0:05 | Dashboard, empty | Click **Load demo** | `A 13-invoice ledger, imported` |
| 0:05-0:12 | Summary cards | Pause. Let the numbers land | `$6,165 at risk. 4 invoices it can act on.` |
| 0:12-0:20 | Report table | Scroll slowly past *Days overdue* and *Stage* | `It declined to touch 9 of them` |
| 0:20-0:27 | Drafts | Open the first draft | `Disputed, opted out, not yet due, already paid` |
| 0:27-0:33 | Same draft | Let the reader read two lines | `The 4 it writes are specific` |
| 0:33-0:38 | Drafts | Click **Approve & send**; row moves to nudge history | `Nothing sends without you` |
| 0:38-0:40 | End card | Cut to a still frame | `dunwell` |

**Read the numbers off the screen, do not type them from this file.** They
move daily - each invoice crosses its own reminder threshold, so a recording
made next week will show different counts and a different at-risk total. The
summary cards are the truth; the captions above are what today's ledger shows.

The 9-of-13 hold is the most persuasive thing on screen, so give it its own
beat. A viewer's first assumption is "AI spams my clients", and the report is
the direct answer: disputed invoices, opted-out customers, missing contacts,
invoices too old to automate, and money already paid are all pulled out and
left for a human. That is the difference between this and a mail-merge.

Timing does not need to be exact. What matters:

- **The first frame is the product**, not a logo, not a title card.
- **One draft gets read.** That is the only moment the viewer sees the actual
  output, and it is what separates this from "AI email writer".
- **The send is shown.** Approve-and-send is the trust moment; skipping it
  leaves the obvious question ("will it email my clients on its own?")
  unanswered, and someone will ask it in the comments.

Do not scroll fast. Slow scrolls read as confidence; fast scrolls read as
hiding something.

---

## 3. Caption and export

Game Bar has no editor. Use **Clipchamp** (built into Windows 11, search it in
the Start menu) or the **Photos** app for a quick trim.

1. Open the clip, trim the head and tail so it starts on the **Load demo**
   click and ends on the finished nudge row.
2. Add the captions from the table above as text at the bottom, one at a time.
   White text, dark translucent box, ~28px.
3. Export **1080p, 30 fps, MP4 (H.264)**.

Under 40 seconds and under ~20 MB means X will upload it natively instead of
choking. That matters: native video gets far more reach than a link.

---

## 4. Publish it in two places

**X** - upload the MP4 directly. Never post a YouTube link as the primary
tweet; native video plays inline.

**Landing page** - set the repository variable and the "watch the demo"
button appears on its own:

```
Settings > Secrets and variables > Actions > Variables
  DUNWELL_DEMO_URL = <YouTube unlisted link, or a hosted .mp4>
```

Then re-run the `Deploy landing to GitHub Pages` workflow. The button is
hidden until that variable exists, so the live page never shows a dead
button.

For the landing page, a **YouTube (unlisted)** link is the lower-effort
option: no file size limit, no bandwidth, and the modal already rewrites
YouTube share links into embeds. Self-hosting the MP4 on Pages works too, but
GitHub Pages caps individual files at 100 MB and throttles large media.

---

## 5. Checklist before you hit record

- [ ] Account exists, signed in, dashboard empty
- [ ] `Load demo` not yet clicked
- [ ] Focus assist on, every other app closed
- [ ] Browser full screen, zoom 100%
- [ ] `127.0.0.1:8906`, not the public URL
- [ ] Game Bar: 30 fps, mic off
- [ ] Do a 5-second throwaway take to confirm it records
- [ ] `Win+Alt+R`
