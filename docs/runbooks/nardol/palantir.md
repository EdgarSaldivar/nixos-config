# Palantír on nardol

Palantír answers questions about videos. It is an OpenAI-compatible endpoint:
GLM-4.6V-Flash plans the work and calls tools that Palantír runs. The tools
are face recognition, speech transcription, search, scene detection and frame
and clip extraction. The module is
[`palantir.nix`](../../../hosts/nixos/nardol/palantir.nix) and the code is in
[`palantir/app`](../../../hosts/nixos/nardol/palantir/app).

## When it runs

Palantír runs only while the `glm-4.6v-flash` inference profile is selected.
It starts and stops with the inference unit, so a game, which stops inference,
stops Palantír too. Under any other profile its unit stays inactive:

```sh
sudo nardol-model switch glm-4.6v-flash   # Palantír starts with it
systemctl status docker-palantir
curl -s http://nardol:8003/health
```

## Ask it something

Any OpenAI client works. Use base URL `http://nardol:8003/v1` and model
`palantir`. Attach a video as a content part; an http(s) URL, a `data:` URL, or
a file under `/srv/palantir/inbox` (or a configured library root) all work:

```sh
curl -s http://nardol:8003/v1/chat/completions -H 'content-type: application/json' -d '{
  "model": "palantir",
  "messages": [{"role": "user", "content": [
    {"type": "text", "text": "What happens in this video, and is Grandma in it?"},
    {"type": "video_url", "video_url": {"url": "file:///data/inbox/1998-xmas.mp4"}}]}]}'
```

The answer's `palantir_trace` lists the tools it called and what they
returned. Videos already added can be named by id (`v_` followed by 12 hex
digits) in later questions.

## Add videos ahead of time

The first question about a video computes its index (scenes, faces,
transcript, search frames). For long videos, add them first and index them in
the background:

```sh
curl -s -X POST http://nardol:8003/v1/videos -F file=@1998-xmas.mp4 -F index=1
curl -s http://nardol:8003/v1/videos
```

## Enroll people

`find_person` can only find people who are enrolled, and it is the only way
Palantír decides who is in a video. Use several clear photos per person, with
some margin around each face. Tight crops are not detected.

```sh
curl -s -X POST http://nardol:8003/v1/people -F name=Grandma \
  -F files=@grandma1.jpg -F files=@grandma2.jpg -F files=@grandma3.jpg
curl -s http://nardol:8003/v1/people
curl -s -X DELETE http://nardol:8003/v1/people/Grandma
```

A face counts as a match at a cosine similarity of 0.45 or higher
(`faceThreshold`). Raise it if strangers are matched, and lower it if the
person is missed.

## Rebuild the runtime image

The image holds only dependencies; the code is mounted from the Nix store. After
changing `palantir/Dockerfile` or `requirements.txt`:

```sh
sudo docker build -t palantir-runtime:N hosts/nixos/nardol/palantir
sudo docker image inspect palantir-runtime:N --format '{{.Id}}'
```

Put the ID in `nardol.palantir.image` and deploy.

## Limits

- GLM-4.6V-Flash is a 9B model. It can slip on multi-step questions, so check
  `palantir_trace` when an answer looks wrong.
- Speech: low-confidence segments are dropped. Music, singing and noise often
  come back as "no clear speech" rather than invented text. Palantír does not
  detect sound events such as laughter or singing yet.
- The archive is not connected. Set `nardol.palantir.library` once it is
  mounted on nardol.
