from pihub.bt_le.controller import HIDTransportBLE


def test_hid_transport_available_safe_without_start() -> None:
    transport = HIDTransportBLE(adapter="hci0", device_name="PiHub Remote")
    assert transport.available is False
