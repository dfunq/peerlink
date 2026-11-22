// peerlink-touchid: asks you to be present with Touch ID, or your Mac password, for the peerlink daemon.
//
// stdin:  {"reason": "...", "nonce": "<hex>", "key": "<hex>"}
// stdout: {"result": "yes" | "no" | "unavailable", "nonce": "<same>", "hmac": HMAC-SHA256(key, nonce + ":" + result)}
//
// The daemon builds this file with swiftc when it starts and its source has changed. It is never shipped as a
// binary. The key is used once and never stored, so an answer that some other program prints can't be signed.

import CryptoKit
import Foundation
import LocalAuthentication

struct Request: Decodable {
    let reason: String
    let nonce: String
    let key: String
}

func hexBytes(_ hex: String) -> Data? {
    guard hex.count % 2 == 0 else { return nil }
    var data = Data(capacity: hex.count / 2)
    var i = hex.startIndex
    while i < hex.endIndex {
        let j = hex.index(i, offsetBy: 2)
        guard let b = UInt8(hex[i..<j], radix: 16) else { return nil }
        data.append(b)
        i = j
    }
    return data
}

func answer(_ result: String, _ req: Request, _ key: Data) -> Never {
    let mac = HMAC<SHA256>.authenticationCode(for: Data("\(req.nonce):\(result)".utf8), using: SymmetricKey(data: key))
    let hex = mac.map { String(format: "%02x", $0) }.joined()
    let out: [String: String] = ["result": result, "nonce": req.nonce, "hmac": hex]
    let json = try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
    FileHandle.standardOutput.write(json)
    FileHandle.standardOutput.write(Data("\n".utf8))
    exit(0)
}

let input = FileHandle.standardInput.readDataToEndOfFile()
guard let req = try? JSONDecoder().decode(Request.self, from: input), let key = hexBytes(req.key), key.count >= 16,
      !req.nonce.isEmpty, !req.reason.isEmpty else {
    FileHandle.standardError.write(Data("peerlink-touchid: expected {reason, nonce, key} on stdin\n".utf8))
    exit(2)
}

let context = LAContext()
var err: NSError?
// deviceOwnerAuthentication: Touch ID, or your password when there's no sensor or it isn't enrolled.
guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &err) else {
    FileHandle.standardError.write(Data("peerlink-touchid: \(err?.localizedDescription ?? "can't ask")\n".utf8))
    answer("unavailable", req, key)
}

let done = DispatchSemaphore(value: 0)
var yes = false
context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: req.reason) { ok, _ in
    yes = ok
    done.signal()
}
// The daemon gives up after 90 s. Close the dialog before that, so no one presses on a question nobody waits for.
if done.wait(timeout: .now() + 80) == .timedOut {
    context.invalidate()
    answer("no", req, key)
}
answer(yes ? "yes" : "no", req, key)
