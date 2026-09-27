# AI Meeting Room Brain Extension (Phase 3A-X POC)

This unpacked Manifest V3 extension is not installed automatically. The user
must load it from `chrome://extensions`, open a ChatGPT conversation, then use
the popup to Pair and Bind the current tab.

It has no cookies, webRequest, debugger, `<all_urls>`, or storage-state
permissions. It only reads/writes the composer and latest response in the
explicitly bound tab. Pairing credentials are local Bridge credentials, not
ChatGPT credentials.

The POC uses loopback HTTP long-polling at `127.0.0.1:9890`; the server refuses
non-extension origins and never binds to `0.0.0.0`.
