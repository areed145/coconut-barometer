# Apple Music setup

This connects the site's **Music** page to your Apple Music account. It takes about 15 minutes, and you need a paid Apple Developer account and an Apple Music subscription. Do the last part on the live site, not a local copy.

## 1. Make a MusicKit key (developer.apple.com)

Go to [Certificates, Identifiers & Profiles](https://developer.apple.com/account/resources/identifiers/list).

1. **Make a Media ID.** Under **Identifiers**, click **+**, choose **Media IDs**, and continue. Enter a description (`Coconut Barometer`) and an identifier (`media.com.yourname.coconutbarometer`), tick **MusicKit**, and register it.
2. **Make the key.** Under **Keys**, click **+** and name it. Tick **Media Services (MusicKit, ShazamKit, Apple Music Feed)**, click **Configure**, pick the Media ID, then save and register.
3. **Download the `.p8` file.** Apple only lets you download it once, so keep it somewhere safe. Note the **Key ID** on that page.
4. **Find your Team ID.** It's under [Account → Membership details](https://developer.apple.com/account#MembershipDetailsCard).

## 2. Add them to the site

In **Settings → Music**, fill in:

| Setting | What goes in it |
| --- | --- |
| Team ID | The 10 characters from Membership details |
| Key ID | The 10 characters from the key's page |
| Private key | Open the `.p8` file in a text editor and paste all of it |

The private key is stored encrypted, like the site's other API keys.

## 3. Connect your account

1. Click **Connect Apple Music** at the top of **Settings → Music**.
2. Enter your password for this site and click the button.
3. Sign in with Apple in the popup and allow access.

You land on **Music → Recent**, which lists the newest plays. The first read comes within a minute or two, and **Sync now** in **Settings → Music** reads straight away at any time. Reads show under Jobs on the **Status** page.

## 4. Add your earlier history (optional)

Apple's hourly list only starts now, but your data export goes back years, with exact times and how long you really listened. Request it at [privacy.apple.com](https://privacy.apple.com) under **Request a copy of your data**, choosing the Apple Media Services information. It can take a few days to arrive.

The export holds IP addresses, locations and account numbers, so never put it in this repository. This step reduces it on your own computer, keeping only song, artist, album, times, how each play ended and what it was played from:

```bash
coconut-barometer music-export \
  "Apple Music Play Activity.csv" \
  "Apple Music - Play History Daily Tracks.csv" \
  -o music-history.csv.gz
```

Both files are in the export's `Apple Music Activity` folder. Then upload `music-history.csv.gz` under **Earlier history** in **Settings → Music**.

- **Artists are matched.** The main file has no artist, so each play takes one from the second file by day and hour, then by title alone. Plays with no match keep their song but don't count towards artists.
- **Genres fill in by themselves.** Once your key is saved, the site looks tracks up in Apple's catalog, 300 a minute. That also replaces any artist matched by title with Apple's own.
- **Skips are left out.** A play counts if it finished or lasted 30 seconds.
- **Importing again replaces the last import**, so you can use a newer export to fill the gap before your first hourly read. Anything after that first read stays with the hourly reads.

## The Music pages

- **Recent** (`#/music/recent`) is the newest 50 plays, with whether Apple Music is connected.
- **Stats** (`#/music/stats`) is one period: totals against the period before, plays per day, then **Most played** (artists, songs, albums, genres), **Patterns** (when you listen, rain or shine) and **Covers and colors** (the color strip, album wall and monthly palettes). **Poster** draws the period as a picture to save.
- **Artists** (`#/music/artists`) is the artist graph, either **Top in this period** or the **Whole graph** (below).

Stats and Artists share one period: the last 7, 30 or 90 days, a year, all of it, or **Custom**, which picks two dates, with **Jump to…** for this or last month, this year, or any earlier year. The period is in the page's address, so a link opens the same view, and the next visit in the same browser starts on the last one picked (30 days the first time).

## Covers and colors

Once your key is saved, the site fills in each track's cover and colors from Apple's catalog, 300 tracks a minute, so a large history takes a couple of hours. That powers the color strip, monthly palettes, album wall, cover thumbnails, the **Poster** button, and the soundtrack on each photo trip. All of it is signed in only.

Your server fetches each cover once and keeps its own copy in `music_art/` next to the database, so the site's security rules stay as they are and Apple's image host never sees your browser.

**Rain or shine** needs weather stations and at least 30 plays on rainy days, so it stays hidden until then.

## Artist network, MusicBrainz credits and artist profiles

**Music → Artists**, **Top in this period** (`#/music/artists`), draws the most played artists of the period and how they connect: collaborations in titles, composers from Apple's catalog, MusicBrainz credits, and profile matches (below). Playing two artists back to back doesn't link them, since that says more about a playlist or shuffle than about the music. Pick the period and how many artists to draw (50 to 200). Scroll or pinch to zoom, drag the background to pan, drag an artist to pull it, click one for its connections, double-click to zoom to its neighbours, or type a name in **Find an artist**. Clicking a cluster or a hidden bridge zooms to it. Artists keep their places when you change the period, the number drawn or the link kinds shown, so you can watch the network reshape.

For real credits (producers, writers, players, featured artists, band members), turn on **Look up credits on MusicBrainz** in **Settings → Music** and give a **Contact for MusicBrainz** (an email address or URL). MusicBrainz asks every app to name a contact, and it's sent with each request. Lookups don't start without one.

Each track is looked up once: by its ISRC, else by title and main artist (kept only when the match is credited to that same artist), most played first. Then come the artists those tracks are credited to, for their bands and collaborations. The lookups run as **MusicBrainz lookup** jobs at the back of the Status page's job line, about a minute of requests each. Every request waits for one shared limit of one a second, which MusicBrainz requires, so a library of a few thousand tracks takes a few hours the first time. **Where the links come from**, below the graph, shows how far along they are, and the graph refreshes as they finish. Lookups are held in local mode.

Each artist lookup also keeps what MusicBrainz knows about the artist: group or person, where they started, their years, and the genres its editors voted for.

### Artist profiles and profile matches

**Profile match** links join artists that are alike on paper. Each artist has a profile: genres (from Apple, MusicBrainz and Claude), scenes, the decades they mattered most in, and where they're from. The site compares profiles itself every time the graph loads, with no AI call. Each link names what the two share, for example *bakersfield sound · country rock · 1960s–70s · Bakersfield*.

- **A rare genre counts for more than a common one.** A genre that more than a quarter of the drawn artists share is ignored, so two artists known only as "Rock" aren't linked. The same goes for a scene that more than a quarter of them share.
- **Years and places alone never link two artists.** A pair needs genres that are clearly alike, or a scene in common. A shared scene links artists from different eras too, so lineage shows (Merle Haggard and Sturgill Simpson through outlaw country).
- **Each artist keeps at most its 4 closest matches**, so the network doesn't turn into a hairball.
- **Clusters are named** after a scene most of their members share, else the genres they have most of.

To fill in scenes, styles and decades, turn on **Profile artists with Claude** in **Settings → Music**. It uses the Anthropic key from **Settings → Vision**. Every artist with over 10 plays is asked about once, most played first, 25 at a time, as **Artist profiles** jobs at the back of the Status page's job line. Claude picks from the labels already in use where one fits, so matching labels are spelled the same. **Requests a day** caps the cost (20 by default; 1,000 artists take 40 requests, so a big library fills in over a few days). New artists are asked about once they pass 10 plays.

**Where the links come from** shows how many artists have profiles, with **Profile more now** to queue a job straight away and **Start over** to forget every profile and ask again, for example after switching models. Profiles are held in local mode.

## The whole graph

**Music → Artists**, **Whole graph** (`#/music/artists?view=whole`), is every artist with over 10 plays and every link between them, all time, rather than the top few hundred of a period. You start somewhere and follow links outward.

- **Start** from one of your most played artists, a scene or a decade on the opening card, or search for an artist, scene, genre, decade or country. A scene, genre, decade or country adds the most played artists that fit, lit up.
- **Grow the map** by clicking an artist: **Show neighbours** adds its strongest links, **Two steps out** adds theirs as well, and **Remove** takes it off. New artists arrive with their links to everyone already shown, and the map makes room for them. **Start over** clears it.
- **How are they connected?** finds the strongest chain of links between two artists and lights it up, with the reason for each step. Artists with no chain between them say so.
- In **Top in this period**, an artist's detail panel has **Explore from here in the whole graph**.
- **Ask about your listening** takes a question in plain words: "What connects my bluegrass and my jazz?", "Which scenes did I get into this year?", "Who links the Grateful Dead and Phish in what I play?". Claude answers by looking things up in your own graph (artists by scene, genre, decade or country; an artist's plays by year and links; the chain between two artists; what you played or first played in a period), then gives a short answer and lights up the artists and links behind it. Only artists in your graph and links that really exist are shown, whatever the answer says. It uses the Anthropic key and model from **Settings → Vision**, costs a few thousand tokens a question, and runs only when you ask.

The site builds the whole graph the first time it's opened (a second or two for a few thousand artists) and keeps it. When new plays, MusicBrainz lookups or profiles change it, the site builds it again in the background, at most every 5 minutes, and the page keeps answering from the graph it has until the new one is ready. While MusicBrainz is filling in credits, the graph can be up to 5 minutes behind. At most 400 artists are shown at once, so the layout stays quick on a phone.

## What to expect

- **Hourly plays start after the first read.** That read only records where your list stands, because Apple doesn't say when those tracks were played.
- **Library songs are included.** Apple's list leaves them out unless asked, and it counts some songs played from search as library songs (live albums especially). They have no genres, and some have no cover, since Apple's catalog can't look them up.
- **Plays from your phone can arrive late.** A phone can send a play to Apple after plays from another device were already read. It's still counted once, at the next read.
- **Times are estimates.** Apple gives no play times, so each play is placed within the hour between two reads. Days and hours are close, not exact.
- **Counts can run low.** Apple can collapse a track played several times in a row into one, and some plays may never appear. A read every hour can also lose plays if you get through more than about 130 tracks between reads.
- **Hours assume whole tracks.** Skipped tracks can count in full.

## When it stops

Apple ends the connection after a while (about six months, by Apple's older guidance, though some developers report less). The Status page then shows **needs connecting again**, and plays made while it's disconnected are lost once they drop off Apple's list. Click **Connect again** in **Settings → Music** to fix it.

If the key itself is revoked, make a new one (step 1), update the Key ID and private key, and connect again.
