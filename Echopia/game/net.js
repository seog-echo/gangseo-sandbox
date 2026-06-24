// WebSocket link to the Echopia backend. Sends control messages
// (avatar state + stimulation) and delivers per-tick payloads.

export function connectBackend({ onTick, onStatus }) {
  let ws = null;
  let lastSent = "";

  function connect() {
    ws = new WebSocket(`ws://${location.host}/`);
    ws.onopen = () => { onStatus?.("connected"); lastSent = ""; };
    ws.onclose = () => { onStatus?.("reconnecting…"); setTimeout(connect, 1000); };
    ws.onerror = () => ws.close();
    ws.onmessage = (e) => {
      try { onTick?.(JSON.parse(e.data)); } catch { /* ignore */ }
    };
  }
  connect();

  // Send control, de-duplicated so we only transmit on actual change.
  function send(control) {
    const s = JSON.stringify(control);
    if (s === lastSent || !ws || ws.readyState !== WebSocket.OPEN) return;
    lastSent = s;
    ws.send(s);
  }

  return { send };
}
