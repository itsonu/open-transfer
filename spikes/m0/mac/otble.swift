// M0 spike (throwaway): Bluetooth LE between this Mac and another device.
//
//   otble serve     advertise "OT-M0" with a GATT service that counts what it receives
//                   and streams bytes back on request
//   otble connect   find "OT-M0", connect, then measure upload and download
//
// OT_BYTES sets the test size (default 256 KiB). The Android spike app speaks the same
// protocol: write "S<n>" then n bytes of data; write "P<n>" to get n bytes back. The
// side receiving data answers "R<bytes>:<ms>" when it has them all.

import CoreBluetooth
import Foundation

let service = CBUUID(string: "7F1C0000-6F70-656E-7472-616E73666572")
let rxUUID = CBUUID(string: "7F1C0001-6F70-656E-7472-616E73666572")
let txUUID = CBUUID(string: "7F1C0002-6F70-656E-7472-616E73666572")
let size = Int(ProcessInfo.processInfo.environment["OT_BYTES"] ?? "") ?? 262_144
let dot = UInt8(ascii: ".")

func log(_ s: String) {
    print(String(format: "[%.3f] ", Date().timeIntervalSince1970.truncatingRemainder(dividingBy: 1000)) + s)
    fflush(stdout)
}

func rate(_ bytes: Int, _ ms: Int) -> String {
    String(format: "%.1f KB/s", Double(bytes) / 1024 / (Double(max(ms, 1)) / 1000))
}

func number(_ d: Data) -> Int { Int(String(decoding: d.dropFirst(), as: UTF8.self)) ?? 0 }

final class Server: NSObject, CBPeripheralManagerDelegate {
    var pm: CBPeripheralManager!
    var tx: CBMutableCharacteristic!
    var central: CBCentral?
    var expect = 0, got = 0, toSend = 0, started = Date()

    override init() {
        super.init()
        pm = CBPeripheralManager(delegate: self, queue: nil)
    }

    func peripheralManagerDidUpdateState(_ p: CBPeripheralManager) {
        log("bluetooth state \(p.state.rawValue) (5 = on, 3 = not allowed)")
        guard p.state == .poweredOn else { return }
        let rx = CBMutableCharacteristic(
            type: rxUUID, properties: [.write, .writeWithoutResponse], value: nil, permissions: [.writeable])
        tx = CBMutableCharacteristic(type: txUUID, properties: [.notify], value: nil, permissions: [.readable])
        let s = CBMutableService(type: service, primary: true)
        s.characteristics = [rx, tx]
        p.add(s)
    }

    func peripheralManager(_ p: CBPeripheralManager, didAdd s: CBService, error: Error?) {
        if let error { log("adding the service failed: \(error)"); exit(1) }
        p.startAdvertising([CBAdvertisementDataServiceUUIDsKey: [service], CBAdvertisementDataLocalNameKey: "OT-M0"])
    }

    func peripheralManagerDidStartAdvertising(_ p: CBPeripheralManager, error: Error?) {
        log(error.map { "advertising failed: \($0)" } ?? "advertising OT-M0")
    }

    func peripheralManager(_ p: CBPeripheralManager, central: CBCentral, didSubscribeTo c: CBCharacteristic) {
        self.central = central
        log("connected; notifications up to \(central.maximumUpdateValueLength) bytes")
    }

    func peripheralManager(_ p: CBPeripheralManager, didReceiveWrite requests: [CBATTRequest]) {
        for r in requests { handle(r.value ?? Data()) }
        p.respond(to: requests[0], withResult: .success)
    }

    func handle(_ d: Data) {
        guard let first = d.first else { return }
        if first == UInt8(ascii: "S") {
            expect = number(d); got = 0; started = Date()
            log("receiving \(expect) bytes")
        } else if first == UInt8(ascii: "P") {
            toSend = number(d); started = Date()
            log("sending \(toSend) bytes")
            pump()
        } else if expect > 0 {
            got += d.count
            if got >= expect {
                let ms = Int(Date().timeIntervalSince(started) * 1000)
                log("received \(got) bytes in \(ms) ms = \(rate(got, ms))")
                _ = pm.updateValue(Data("R\(got):\(ms)".utf8), for: tx, onSubscribedCentrals: nil)
                expect = 0
            }
        }
    }

    func pump() {
        guard let c = central else { return }
        while toSend > 0 {
            let chunk = Data(repeating: dot, count: min(c.maximumUpdateValueLength, toSend))
            if !pm.updateValue(chunk, for: tx, onSubscribedCentrals: nil) { return }  // resumes when ready
            toSend -= chunk.count
        }
    }

    func peripheralManagerIsReady(toUpdateSubscribers p: CBPeripheralManager) { pump() }
}

final class Client: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    var cm: CBCentralManager!
    var peer: CBPeripheral?
    var rx: CBCharacteristic?
    var phase = 0, toWrite = 0, got = 0, started = Date()  // 1 upload, 2 download, 3 done

    override init() {
        super.init()
        cm = CBCentralManager(delegate: self, queue: nil)
    }

    func centralManagerDidUpdateState(_ c: CBCentralManager) {
        log("bluetooth state \(c.state.rawValue) (5 = on, 3 = not allowed)")
        if c.state == .poweredOn { c.scanForPeripherals(withServices: [service]); log("scanning for OT-M0") }
    }

    func centralManager(_ c: CBCentralManager, didDiscover p: CBPeripheral,
                        advertisementData a: [String: Any], rssi: NSNumber) {
        log("found \(a[CBAdvertisementDataLocalNameKey] ?? p.name ?? "?"), signal \(rssi) dBm")
        c.stopScan()
        peer = p
        p.delegate = self
        c.connect(p)
    }

    func centralManager(_ c: CBCentralManager, didConnect p: CBPeripheral) {
        log("connected")
        p.discoverServices([service])
    }

    func centralManager(_ c: CBCentralManager, didFailToConnect p: CBPeripheral, error: Error?) {
        log("connecting failed: \(String(describing: error))"); exit(1)
    }

    func centralManager(_ c: CBCentralManager, didDisconnectPeripheral p: CBPeripheral, error: Error?) {
        log("disconnected"); exit(phase == 3 ? 0 : 1)
    }

    func peripheral(_ p: CBPeripheral, didDiscoverServices error: Error?) {
        p.services?.forEach { p.discoverCharacteristics([rxUUID, txUUID], for: $0) }
    }

    func peripheral(_ p: CBPeripheral, didDiscoverCharacteristicsFor s: CBService, error: Error?) {
        for c in s.characteristics ?? [] {
            if c.uuid == rxUUID { rx = c }
            if c.uuid == txUUID { p.setNotifyValue(true, for: c) }
        }
    }

    func peripheral(_ p: CBPeripheral, didUpdateNotificationStateFor c: CBCharacteristic, error: Error?) {
        log("writes up to \(p.maximumWriteValueLength(for: .withoutResponse)) bytes")
        phase = 1; toWrite = size
        p.writeValue(Data("S\(size)".utf8), for: rx!, type: .withResponse)
    }

    func peripheral(_ p: CBPeripheral, didWriteValueFor c: CBCharacteristic, error: Error?) {
        if phase == 1 && toWrite == size { started = Date(); pump() }  // the "S" header landed
    }

    func pump() {
        guard let p = peer, let rx else { return }
        let n = p.maximumWriteValueLength(for: .withoutResponse)
        while toWrite > 0 && p.canSendWriteWithoutResponse {
            let k = min(n, toWrite)
            p.writeValue(Data(repeating: dot, count: k), for: rx, type: .withoutResponse)
            toWrite -= k
        }
    }

    func peripheralIsReady(toSendWriteWithoutResponse p: CBPeripheral) { if phase == 1 { pump() } }

    func peripheral(_ p: CBPeripheral, didUpdateValueFor c: CBCharacteristic, error: Error?) {
        guard let d = c.value, let first = d.first else { return }
        let ms = Int(Date().timeIntervalSince(started) * 1000)
        if first == UInt8(ascii: "R") && phase == 1 {
            log("UPLOAD   \(size) bytes in \(ms) ms = \(rate(size, ms)) (other side: \(String(decoding: d, as: UTF8.self)))")
            phase = 2; got = 0; started = Date()
            p.writeValue(Data("P\(size)".utf8), for: rx!, type: .withResponse)
        } else if phase == 2 {
            got += d.count
            if got >= size {
                log("DOWNLOAD \(got) bytes in \(ms) ms = \(rate(got, ms))")
                phase = 3
                cm.cancelPeripheralConnection(p)
            }
        }
    }
}

let mode = CommandLine.arguments.dropFirst().first ?? ""
var keep: AnyObject?
switch mode {
case "serve": keep = Server()
case "connect": keep = Client()
default: print("usage: otble serve|connect   (OT_BYTES=262144)"); exit(64)
}
DispatchQueue.main.asyncAfter(deadline: .now() + (mode == "serve" ? 900 : 120)) { log("timed out"); exit(2) }
RunLoop.main.run()
