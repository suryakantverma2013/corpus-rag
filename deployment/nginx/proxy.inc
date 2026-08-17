# Shared upstream-proxy directives (T-605).
#
# The extension is `.inc`, NOT `.conf`, deliberately: nginx's default configuration
# auto-includes /etc/nginx/conf.d/*.conf at the `http` level, and a bare set of `proxy_*`
# directives there is a configuration error that stops the container from starting.

proxy_http_version 1.1;

# Must accompany HTTP/1.1: without it nginx forwards the client's own Connection header,
# which can negotiate close-per-response behaviour and defeat both keepalive and streaming.
proxy_set_header Connection        "";

proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host  $host;
proxy_set_header X-Forwarded-Port  $server_port;

# Streaming settings deliberately live in streaming.inc rather than here, and are included
# only by the one location that carries streams. nginx treats a REPEATED directive in the
# same block as a fatal configuration error ("proxy_buffering directive is duplicate") — it
# does not let a later occurrence win — so a location that includes this file cannot then
# override anything it sets.
