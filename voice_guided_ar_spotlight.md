# 🔗 The Unifying Solution: Voice-Guided AR Spotlight
## Linking the Dashboard, Voice LLM, and Mobile Camera via Qualcomm AI Hub SAM 2

If your dashboard shows the battery telemetry and your voice model answers user questions, judges will ask: 
**"How does the user bridge the gap between what the dashboard says, what the voice explains, and the physical battery pack in front of them?"**

The solution is the **Voice-Guided AR Spotlight (Conversational AR Diagnostics)**. It acts as the single bridge connecting the **User Dashboard**, the **Voice LLM**, and the **Mobile AR View** into one unified, interactive workflow.

---

## 🔄 The Telemetry-to-Voice-to-AR Flow

```
   ┌───────────────────────────────────────────────────────────┐
   │ 1. Telemetry Ingestion (Dashboard DB)                     │
   │    • Cell 3 voltage drops to 2.4V                         │
   └─────────────┬─────────────────────────────────────────────┘
                 │
                 ▼
   ┌───────────────────────────────────────────────────────────┐
   │ 2. Voice Query (Local LLM)                                │
   │    • User talks to phone: "Identify the bad cell."       │
   │    • LLM links query with Dashboard Cell 3 telemetry      │
   └─────────────┬─────────────────────────────────────────────┘
                 │ (Bluetooth Protocol: {"focus_cell": 3})
                 ▼
   ┌───────────────────────────────────────────────────────────┐
   │ 3. Spatial AR Spotlight (Qualcomm AI Hub SAM 2)           │
   │    • Camera feeds video on OnePlus 15                     │
   │    • SAM 2 segments the battery cells                      │
   │    • Spotlight overlay paints physically on Cell 3 only  │
   └───────────────────────────────────────────────────────────┘
```

---

## 🛠️ Implementation: Connecting the Pieces

To implement this bridge, the conversation engine (local LLM) on the Snapdragon PC must emit a structural **"Focus Directive"** key in its payload. The mobile app parses this target key and instructs the SAM 2 custom painter to spotlight only that cell.

### 1. Updated Host-to-Mobile Packet Format
When the user asks the voice assistant a question, the response envelope sent over Bluetooth includes the targeted cell index:

```json
{
  "type": "voice_response",
  "audio_text": "Cell 3 is running hot at 58 degrees and showing 2.4V. I have spotlighted it on your camera.",
  "ar_action": {
    "focus_cell_index": 3,      // <-- Tells SAM 2 which segment to illuminate
    "alert_level": "CRITICAL",   // Paint Crimson
    "overlay_text": "CELL 3: 2.42V | 58.2°C"
  }
}
```

### 2. Mobile App Integration (`mobile_sam2_ar_pipeline.dart`)
In the custom painter, replace the blind "render all cells" logic with a **directed spotlight filter** that triggers only when the Voice LLM target maps to a specific index:

```dart
class CellMaskPainter extends CustomPainter {
  final List<List<Offset>> segments;
  final int? voiceFocusCellIndex; // Passed from the Voice LLM ar_action
  final String? overlayText;
  
  CellMaskPainter({
    required this.segments,
    this.voiceFocusCellIndex,
    this.overlayText,
  });

  @override
  void paint(Canvas canvas, Size size) {
    if (voiceFocusCellIndex == null) return; // Silent if no active voice query target

    for (int i = 0; i < segments.length; i++) {
      // Highlight ONLY the cell that the User Voice + Dashboard flagged
      if (i != voiceFocusCellIndex) continue;

      final points = segments[i];
      if (points.isEmpty) continue;

      // Draw AR Spotlight overlay surrounding the target cell
      final paint = Paint()
        ..color = Colors.red.withOpacity(0.55)
        ..style = PaintingStyle.fill;

      final path = Path()..moveTo(points.first.dx, points.first.dy);
      for (var point in points) {
        path.lineTo(point.dx, point.dy);
      }
      path.close();

      canvas.drawPath(path, paint);

      // Render the floating tech text card directly over the spotlighted cell
      if (overlayText != null) {
        final textSpan = TextSpan(
          text: "🔍 $overlayText",
          style: const TextStyle(
             color: Colors.white,
             fontSize: 12,
             fontWeight: FontWeight.bold,
             backgroundColor: Colors.black85
          ),
        );
        final textPainter = TextPainter(text: textSpan, textDirection: TextDirection.ltr)..layout();
        textPainter.paint(canvas, Offset(points.first.dx, points.first.dy - 16));
      }
    }
  }
  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => true;
}
```

---

## 🏆 The Hackathon Value Pitch for Judges

* **The Problem:** Dashboards present raw tables/numbers, and voice models speak explanations. Neither connects directly to the physical battery. A technician still has to manually decode which wire is Cell 3.
* **The Solution:** EV Guardian's **Voice-Guided AR Spotlight**. By fusing the spoken user query, the vehicle dashboard telemetry, and Qualcomm AI Hub's **SAM 2** segmentation model on-device, the phone visually highlights the exact physical component mentioned in the voice response. 
* **The Snapdragon Advantage:** The speech translation, telemetry analysis, and visual cell tracking run **simultaneously across Snapdragon's heterogeneous computing cores** fully offline (CPU handles Bluetooth, Hexagon NPU accelerates SAM 2, GPU renders the UI overlay).
