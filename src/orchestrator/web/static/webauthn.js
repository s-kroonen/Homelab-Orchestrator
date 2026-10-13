// Passkey ceremony helpers: WebAuthn needs real ArrayBuffers for challenge /
// credential-id fields, but JSON can only carry strings — the server sends
// and expects base64url for all of those, so every call here converts one
// way or the other. No framework, no build step, matches this project's
// plain-Jinja2-plus-vanilla-JS style (see maintenance.html's refresh meta
// tag — that's the ceiling on cleverness this project wants client-side).

function b64urlToBuffer(value) {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/");
  const pad = padded.length % 4 === 0 ? "" : "=".repeat(4 - (padded.length % 4));
  const binary = atob(padded + pad);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function bufferToB64url(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `${resp.status} ${resp.statusText}`);
  }
  return resp.status === 204 ? null : resp.json();
}

function prepareCreationOptions(options) {
  options.challenge = b64urlToBuffer(options.challenge);
  options.user.id = b64urlToBuffer(options.user.id);
  for (const cred of options.excludeCredentials || []) cred.id = b64urlToBuffer(cred.id);
  return options;
}

function prepareRequestOptions(options) {
  options.challenge = b64urlToBuffer(options.challenge);
  for (const cred of options.allowCredentials || []) cred.id = b64urlToBuffer(cred.id);
  return options;
}

function registrationToJSON(credential) {
  const response = credential.response;
  return {
    id: credential.id,
    rawId: bufferToB64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: bufferToB64url(response.clientDataJSON),
      attestationObject: bufferToB64url(response.attestationObject),
      transports: response.getTransports ? response.getTransports() : [],
    },
  };
}

function assertionToJSON(credential) {
  const response = credential.response;
  return {
    id: credential.id,
    rawId: bufferToB64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: bufferToB64url(response.clientDataJSON),
      authenticatorData: bufferToB64url(response.authenticatorData),
      signature: bufferToB64url(response.signature),
      userHandle: response.userHandle ? bufferToB64url(response.userHandle) : null,
    },
  };
}

async function loginWithPasskey(username) {
  const options = await postJSON("/api/auth/login/begin", { username });
  const credential = await navigator.credentials.get({ publicKey: prepareRequestOptions(options) });
  return postJSON("/api/auth/login/complete", { credential: assertionToJSON(credential) });
}

async function registerPasskey({ username, device_label, invite_token }) {
  const options = await postJSON("/api/auth/register/begin", { username, invite_token });
  const credential = await navigator.credentials.create({
    publicKey: prepareCreationOptions(options),
  });
  return postJSON("/api/auth/register/complete", {
    username,
    device_label,
    invite_token,
    credential: registrationToJSON(credential),
  });
}

async function addDevicePasskey(device_label) {
  const options = await postJSON("/api/auth/credentials/add/begin", undefined);
  const credential = await navigator.credentials.create({
    publicKey: prepareCreationOptions(options),
  });
  return postJSON("/api/auth/credentials/add/complete", {
    device_label,
    credential: registrationToJSON(credential),
  });
}
