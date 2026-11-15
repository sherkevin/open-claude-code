# Candidate static asset compression

The Tencent candidate route serves public Vite assets through a separate nginx
location. Keep this more specific than `/zhixing-next/`; never add compression
or proxy buffering to the API / event stream location as part of this change.

```nginx
location ^~ /zhixing-next/assets/ {
    proxy_pass http://127.0.0.1:18092/assets/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    gzip on;
    gzip_types application/javascript text/javascript text/css;
    gzip_min_length 1024;
    gzip_comp_level 5;
    gzip_vary on;
    proxy_buffering on;
}
```

Before applying, back up the containing configuration privately, preserve every
other location, run `nginx -t`, then reload. Roll back the exact saved block if
validation fails. This is not a replacement server configuration.

Verify with browser network resource entries: `encodedBodySize` should be below
`decodedBodySize` for JavaScript, and `Content-Encoding: gzip` must be negotiated.
Check the candidate and original health routes afterward. Transfer byte reduction
does not measure model latency. Keep API and SSE uncompressed/unbuffered under
their existing configuration, and never expose private deployment backups as web
assets.
