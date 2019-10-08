import time
import json
import _thread
import threading
from scapy.all import Dot11, Dot11FCS, Dot11Elt, RadioTap, sendp, sniff

import core
import pwnagotchi
import pwnagotchi.ui.faces as faces

from pwnagotchi.mesh import get_identity
import pwnagotchi.mesh.wifi as wifi
from pwnagotchi.mesh import new_session_id
from pwnagotchi.mesh.peer import Peer


def _dummy_peer_cb(peer):
    pass


class Advertiser(object):
    MAX_STALE_TIME = 300

    def __init__(self, iface, name, version, identity, period=0.3, data={}):
        self._iface = iface
        self._period = period
        self._running = False
        self._stopped = threading.Event()
        self._peers_lock = threading.Lock()
        self._adv_lock = threading.Lock()
        self._new_peer_cb = _dummy_peer_cb
        self._lost_peer_cb = _dummy_peer_cb
        self._peers = {}
        self._frame = None
        self._me = Peer(new_session_id(), 0, 0, {
            'name': name,
            'version': version,
            'identity': identity,
            'face': faces.FRIEND,
            'pwnd_run': 0,
            'pwnd_tot': 0,
            'uptime': 0,
            'epoch': 0,
            'data': data
        })
        self.update()

    def update(self, values={}):
        with self._adv_lock:
            for field, value in values.items():
                self._me.adv[field] = value
            self._frame = wifi.encapsulate(payload=json.dumps(self._me.adv), addr_from=self._me.session_id)

    def on_peer(self, new_cb, lost_cb):
        self._new_peer_cb = new_cb
        self._lost_peer_cb = lost_cb

    def on_face_change(self, old, new):
        # core.log("face change: %s -> %s" % (old, new))
        self.update({'face': new})

    def start(self):
        self._running = True
        _thread.start_new_thread(self._sender, ())
        _thread.start_new_thread(self._listener, ())
        _thread.start_new_thread(self._pruner, ())

    def num_peers(self):
        with self._peers_lock:
            return len(self._peers)

    def peers(self):
        with self._peers_lock:
            return list(self._peers.values())

    def closest_peer(self):
        closest = None
        with self._peers_lock:
            for ident, peer in self._peers.items():
                if closest is None or peer.is_closer(closest):
                    closest = peer
        return closest

    def stop(self):
        self._running = False
        self._stopped.set()

    def _sender(self):
        core.log("started advertiser thread (period:%s sid:%s) ..." % (str(self._period), self._me.session_id))
        while self._running:
            try:
                sendp(self._frame, iface=self._iface, verbose=False, count=5, inter=self._period)
            except Exception as e:
                core.log("error: %s" % e)
            time.sleep(self._period)

    def _on_advertisement(self, src_session_id, channel, rssi, adv):
        ident = adv['identity']
        with self._peers_lock:
            if ident not in self._peers:
                peer = Peer(src_session_id, channel, rssi, adv)
                core.log("detected unit %s (v%s) on channel %d (%s dBm) [sid:%s pwnd_tot:%d uptime:%d]" % ( \
                    peer.full_name(),
                    peer.version(),
                    channel,
                    rssi,
                    src_session_id,
                    peer.pwnd_total(),
                    peer.uptime()))

                self._peers[ident] = peer
                self._new_peer_cb(peer)
            else:
                self._peers[ident].update(src_session_id, channel, rssi, adv)

    def _parse_identity(self, radio, dot11, dot11elt):
        payload = b''
        while dot11elt:
            payload += dot11elt.info
            dot11elt = dot11elt.payload.getlayer(Dot11Elt)

        if payload != b'':
            adv = json.loads(payload)
            self._on_advertisement( \
                dot11.addr3,
                wifi.freq_to_channel(radio.Channel),
                radio.dBm_AntSignal,
                adv)

    def _is_broadcasted_advertisement(self, dot11):
        # dst bcast + protocol signature + not ours
        return dot11 is not None and \
               dot11.addr1 == wifi.BroadcastAddress and \
               dot11.addr2 == wifi.SignatureAddress and \
               dot11.addr3 != self._me.session_id

    def _is_frame_for_us(self, dot11):
        # dst is us + protocol signature + not ours (why would we send a frame to ourself anyway?)
        return dot11 is not None and \
               dot11.addr1 == self._me.session_id and \
               dot11.addr2 == wifi.SignatureAddress and \
               dot11.addr3 != self._me.session_id

    def _on_packet(self, p):
        # https://github.com/secdev/scapy/issues/1590
        if p.haslayer(Dot11):
            dot11 = p[Dot11]
        elif p.haslayer(Dot11FCS):
            dot11 = p[Dot11FCS]
        else:
            dot11 = None

        if self._is_broadcasted_advertisement(dot11):
            try:
                dot11elt = p.getlayer(Dot11Elt)
                if dot11elt.ID == wifi.Dot11ElemID_Identity:
                    self._parse_identity(p[RadioTap], dot11, dot11elt)

                else:
                    raise Exception("unknown frame id %d" % dot11elt.ID)

            except Exception as e:
                core.log("error decoding packet from %s: %s" % (dot11.addr3, e))

    def _listener(self):
        # core.log("started advertisements listener ...")
        expr = "type mgt subtype beacon and ether src %s" % wifi.SignatureAddress
        sniff(iface=self._iface, filter=expr, prn=self._on_packet, store=0, stop_filter=lambda x: self._stopped.isSet())

    def _pruner(self):
        while self._running:
            time.sleep(10)
            with self._peers_lock:
                stale = []
                for ident, peer in self._peers.items():
                    inactive_for = peer.inactive_for()
                    if inactive_for >= Advertiser.MAX_STALE_TIME:
                        core.log("peer %s lost (inactive for %ds)" % (peer.full_name(), inactive_for))
                        self._lost_peer_cb(peer)
                        stale.append(ident)

                for ident in stale:
                    del self._peers[ident]


class AsyncAdvertiser(object):
    def __init__(self, config, view):
        self._config = config
        self._view = view
        self._public_key, self._identity = get_identity(config)
        self._advertiser = None

    def start_advertising(self):
        _thread.start_new_thread(self._adv_worker, ())

    def _adv_worker(self):
        # this will take some time due to scapy being slow to be imported ...
        from pwnagotchi.mesh.advertise import Advertiser

        self._advertiser = Advertiser(
            self._config['main']['iface'],
            pwnagotchi.name(),
            pwnagotchi.version,
            self._identity,
            period=0.3,
            data=self._config['personality'])

        self._advertiser.on_peer(self._on_new_unit, self._on_lost_unit)

        if self._config['personality']['advertise']:
            self._advertiser.start()
            self._view.on_state_change('face', self._advertiser.on_face_change)
        else:
            core.log("advertising is disabled")

    def _on_new_unit(self, peer):
        self._view.on_new_peer(peer)

    def _on_lost_unit(self, peer):
        self._view.on_lost_peer(peer)
