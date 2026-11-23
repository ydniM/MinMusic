# MinMusic

New releases from artists I listen to, checked daily.

**Feed:** https://ydnim.github.io/MinMusic/feed.xml

## Artists.txt

One artist per line: the name, then any links, separated by `|`.

```
Artist Name | https://www.deezer.com/artist/12345
Artist Name | https://www.deezer.com/artist/12345 | https://www.deezer.com/artist/67890
Artist Name | https://www.deezer.com/artist/12345 | https://musicbrainz.org/artist/<id>
```

- Add every Deezer page the artist has (some are split across a few).
- A MusicBrainz link also checks MusicBrainz for releases Deezer is missing.
- Lines starting with `#` are ignored.

Saving the file updates the feed within a few minutes.
