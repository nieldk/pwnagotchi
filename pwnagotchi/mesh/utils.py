import _thread
import logging

import pwnagotchi
import pwnagotchi.plugins as plugins


class AsyncAdvertiser(object):
    def __init__(self, config, view, keypair):
        self._config = config
        self._view = view
        self._keypair = keypair
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
            self._keypair.fingerprint,
            period=0.3,
            data=self._config['personality'])

        self._advertiser.on_peer(self._on_new_unit, self._on_lost_unit)

        if self._config['personality']['advertise']:
            self._advertiser.start()
            self._view.on_state_change('face', self._advertiser.on_face_change)
        else:
            logging.warning("advertising is disabled")

    def _on_new_unit(self, peer):
        self._view.on_new_peer(peer)
        plugins.on('peer_detected', self, peer)

    def _on_lost_unit(self, peer):
        self._view.on_lost_peer(peer)
        plugins.on('peer_lost', self, peer)
