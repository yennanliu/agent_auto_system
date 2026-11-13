// The un-wrapped `fetch`, captured before auth.js installs its 401 → login wrapper on
// `window.fetch`. Used for the calls that must not trigger the login overlay.

export const _origFetch = window.fetch.bind(window);
