import CryptoKit
import Foundation

let input = FileHandle.standardInput.readDataToEndOfFile()
let keySize = 32
guard input.count >= keySize, CommandLine.arguments.count == 2 else { exit(1) }
let key = SymmetricKey(data: input.prefix(keySize))
let payload = Data(input.dropFirst(keySize))
do {
    let result: Data
    switch CommandLine.arguments[1] {
    case "seal":
        guard let combined = try AES.GCM.seal(payload, using: key).combined else { exit(1) }
        result = combined
    case "open":
        result = try AES.GCM.open(AES.GCM.SealedBox(combined: payload), using: key)
    default:
        exit(1)
    }
    FileHandle.standardOutput.write(result)
} catch {
    exit(1)
}
