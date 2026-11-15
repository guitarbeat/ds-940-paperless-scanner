import Foundation
import Vision
import PDFKit
import AppKit

struct PageResult: Codable {
    let page: Int
    let text: String
}

struct OCRResult: Codable {
    let pageCount: Int
    let pages: [PageResult]
    let fullText: String
}

func performOCR(on pdfPath: String, recognitionLevel: VNRequestTextRecognitionLevel = .accurate) -> OCRResult? {
    guard let doc = PDFDocument(url: URL(fileURLWithPath: pdfPath)) else {
        return nil
    }
    
    var pageResults: [PageResult] = []
    var allText: [String] = []
    
    for i in 0..<doc.pageCount {
        guard let page = doc.page(at: i) else { continue }
        let bounds = page.bounds(for: .mediaBox)
        let image = page.thumbnail(of: bounds.size, for: .mediaBox)
        guard let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }
        
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = recognitionLevel
        request.usesLanguageCorrection = true
        
        let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
        do {
            try handler.perform([request])
            let observations = request.results ?? []
            let pageText = observations.compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
            pageResults.append(PageResult(page: i + 1, text: pageText))
            if !pageText.isEmpty {
                allText.append(pageText)
            }
        } catch {
            pageResults.append(PageResult(page: i + 1, text: ""))
        }
    }
    
    return OCRResult(pageCount: doc.pageCount, pages: pageResults, fullText: allText.joined(separator: "\n\n"))
}

guard CommandLine.arguments.count > 1 else {
    fputs("Usage: ds940-ocr <input-pdf-path>\n", stderr)
    exit(1)
}

let pdfPath = CommandLine.arguments[1]
guard let result = performOCR(on: pdfPath) else {
    fputs("Error: Could not open or process PDF at \(pdfPath)\n", stderr)
    exit(1)
}

let encoder = JSONEncoder()
encoder.outputFormatting = [.prettyPrinted]
if let data = try? encoder.encode(result), let jsonString = String(data: data, encoding: .utf8) {
    print(jsonString)
} else {
    fputs("Error: Failed to encode OCR results to JSON\n", stderr)
    exit(1)
}
