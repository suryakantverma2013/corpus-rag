# Server-Sent Events (T-605). Included ONLY by the /api/ location, because these are the
# only endpoints that stream.
#
#   GET  /api/v1/documents/events            (the live document-status channel)
#   POST /api/v1/conversations/{id}/messages (a chat turn)
#   POST /api/v1/messages/{id}/regenerate
#
# With buffering on, nginx holds the frames until the response completes — which for these
# endpoints is "not until the user closes the tab". The document list would stop updating
# and a chat turn would deliver its stage frames and its answer in one lump at the end.
# This is the single most important thing in the nginx configuration, and the easiest to
# lose in a refactor.
proxy_buffering off;
proxy_cache off;

# proxy_read_timeout bounds the IDLE gap between reads, not the total response duration.
# The real gap here is small — the document channel polls on a ~1.5 s interval and the chat
# stream sends its own keepalives — but the 60 s default would still cut off a long
# generation mid-answer.
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
