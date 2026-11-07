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

You land on the **Music** page. The first read comes within a minute or two, and **Sync now** in **Settings → Music** reads straight away at any time. Reads show under Jobs on the **Status** page.

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

## Covers and colors

Once your key is saved, the site fills in each track's cover and colors from Apple's catalog, 300 tracks a minute, so a large history takes a couple of hours. That powers the color strip, monthly palettes, album wall, cover thumbnails, the **Poster** button, and the soundtrack on each photo trip. All of it is signed in only.

Your server fetches each cover once and keeps its own copy in `music_art/` next to the database, so the site's security rules stay as they are and Apple's image host never sees your browser.

**Rain or shine** needs weather stations and at least 30 plays on rainy days, so it stays hidden until then.

## Artist network and MusicBrainz credits

**Music → Artist network** (`#/music/graph`) draws your most played artists and how they connect: played back to back, collaborations in titles, composers from Apple's catalog, and, if you ask, links Claude suggests. Pick the range and how many artists to draw (50 to 200). Scroll or pinch to zoom, drag the background to pan, drag an artist to pull it, click one for its connections, double-click to zoom to its neighbours, or type a name in **Find an artist**. Clicking a cluster or a hidden bridge zooms to it. Artists keep their places when you change the range, the number drawn or the link kinds shown, so you can watch the network reshape.

For real credits (producers, writers, players, featured artists, band members), turn on **Look up credits on MusicBrainz** in **Settings → Music** and give a **Contact for MusicBrainz** (an email address or URL). MusicBrainz asks every app to name a contact, and it's sent with each request. Lookups don't start without one.

Each track is looked up once: by its ISRC, else by title and main artist (kept only when the match is credited to that same artist), most played first. Then come the artists those tracks are credited to, for their bands and collaborations. The lookups run as **MusicBrainz lookup** jobs at the back of the Status page's job line, about a minute of requests each. Every request waits for one shared limit of one a second, which MusicBrainz requires, so a library of a few thousand tracks takes a few hours the first time. The graph page shows how far along they are and refreshes as they finish. Lookups are held in local mode.

## What to expect

- **Hourly plays start after the first read.** That read only records where your list stands, because Apple doesn't say when those tracks were played.
- **Check for missing plays first.** Play something from your library and from one of your own playlists, wait for the next read, and see whether they show up. Developers have reported these plays missing from Apple's API for some accounts, so this tells you how complete your numbers will be.
- **Times are estimates.** Apple gives no play times, so each play is placed within the hour between two reads. Days and hours are close, not exact.
- **Counts can run low.** Apple can collapse a track played several times in a row into one, and some plays may never appear. A read every hour can also lose plays if you get through more than about 130 tracks between reads.
- **Hours assume whole tracks.** Skipped tracks can count in full.

## When it stops

Apple ends the connection after a while (about six months, by Apple's older guidance, though some developers report less). The Status page then shows **needs connecting again**, and plays made while it's disconnected are lost once they drop off Apple's list. Click **Connect again** in **Settings → Music** to fix it.

If the key itself is revoked, make a new one (step 1), update the Key ID and private key, and connect again.
