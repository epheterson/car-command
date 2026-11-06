// Passkeys in the browser (WebAuthn): the site's options as navigator.credentials takes them, and the phone's
// answers as the site's webauthn library reads them. Loaded before commands.js on Now (a tier 3 tap) and by
// Settings › Access (adding one, confirming a change). Pure, so the tests run it in node.
var CCPasskey = {
  // base64url, as WebAuthn's JSON carries bytes
  fromB64u: function (text) {
    var t = String(text), b = atob(t.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((t.length + 3) % 4));
    var out = new Uint8Array(b.length);
    for (var i = 0; i < b.length; i++) out[i] = b.charCodeAt(i);
    return out.buffer;
  },
  toB64u: function (buf) {
    var bytes = new Uint8Array(buf), b = "";
    for (var i = 0; i < bytes.length; i++) b += String.fromCharCode(bytes[i]);
    return btoa(b).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  },
  ids: function (list) {
    return (list || []).map(function (c) { return { type: c.type || "public-key", id: CCPasskey.fromB64u(c.id), transports: c.transports }; });
  },
  // options for navigator.credentials.get: confirming a command or a change
  request: function (o) {
    return { challenge: CCPasskey.fromB64u(o.challenge), rpId: o.rpId, timeout: o.timeout, userVerification: o.userVerification, allowCredentials: CCPasskey.ids(o.allowCredentials) };
  },
  // options for navigator.credentials.create: a new passkey
  creation: function (o) {
    return {
      challenge: CCPasskey.fromB64u(o.challenge), rp: o.rp, timeout: o.timeout, attestation: o.attestation,
      user: { id: CCPasskey.fromB64u(o.user.id), name: o.user.name, displayName: o.user.displayName },
      pubKeyCredParams: o.pubKeyCredParams, authenticatorSelection: o.authenticatorSelection, excludeCredentials: CCPasskey.ids(o.excludeCredentials),
    };
  },
  answer: function (cred) {
    var r = cred.response;
    return {
      id: cred.id, rawId: CCPasskey.toB64u(cred.rawId), type: cred.type,
      response: { clientDataJSON: CCPasskey.toB64u(r.clientDataJSON), authenticatorData: CCPasskey.toB64u(r.authenticatorData), signature: CCPasskey.toB64u(r.signature), userHandle: r.userHandle ? CCPasskey.toB64u(r.userHandle) : null },
    };
  },
  made: function (cred) {
    var r = cred.response;
    return {
      id: cred.id, rawId: CCPasskey.toB64u(cred.rawId), type: cred.type,
      response: { clientDataJSON: CCPasskey.toB64u(r.clientDataJSON), attestationObject: CCPasskey.toB64u(r.attestationObject), transports: r.getTransports ? r.getTransports() : [] },
    };
  },
  // a sheet the person closed is no failure: nothing is said
  quiet: function (err) {
    return !!err && (err.name === "NotAllowedError" || err.name === "AbortError");
  },
};
