/* Test-only event fixtures. No capture, frames, network or production import. */
(function () {
  var scenario = new URLSearchParams(location.search).get('fixture');
  if (!scenario) return;
  window.history.replaceState(null, '', location.pathname + location.search + '#token=local-test-only');
  window.WebSocket = class {
    static OPEN = 1;
    constructor() { this.readyState = 1; setTimeout(() => this.onopen && this.onopen(), 30); }
    send(raw) {
      var msg = JSON.parse(raw);
      if (msg.type !== 'auth') return;
      var payload = scenario === 'busy' ? {type:'error',code:'busy'} :
        scenario === 'capture' ? {type:'error',code:'capture_unavailable'} : {type:'ready'};
      setTimeout(() => this.onmessage && this.onmessage({data:JSON.stringify(payload)}), 30);
    }
    close() { this.readyState = 3; }
  };
  window.RTCPeerConnection = class {
    constructor() { this.iceGatheringState = 'complete'; this.localDescription = {sdp:'fixture-only'}; }
    addTransceiver() {}
    createOffer() { return Promise.resolve({}); }
    setLocalDescription() {
      setTimeout(() => {
        this.connectionState = 'connected';
        this.onconnectionstatechange();
        this.ontrack({streams:[new MediaStream()]});
      }, 30);
      return Promise.resolve();
    }
    close() {}
  };
  var attempts = 0;
  HTMLMediaElement.prototype.play = function () {
    attempts++;
    if (attempts === 1) return Promise.reject(new DOMException('Fixture autoplay refusal', 'NotAllowedError'));
    this.dispatchEvent(new Event('playing'));
    return Promise.resolve();
  };
})();
