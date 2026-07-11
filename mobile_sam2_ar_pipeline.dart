import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';
import 'dart:ui' as ui;
import 'package:flutter/material.dart';
import 'package:camera/camera.dart';
import 'package:tflite_flutter/tflite_flutter.dart';

/// ============================================================================
/// EV GUARDIAN — Qualcomm AI Hub SAM 2 Mobile AR Diagnostic Pipeline (Flutter)
/// ============================================================================
/// This file implements the application-level logic to execute Qualcomm AI Hub 
/// optimized Segment Anything Model 2 (SAM 2) on the OnePlus 15 (Snapdragon NPU) 
/// and overlay live Bluetooth telemetry onto the physical segmented battery cells.
/// ============================================================================

class Sam2DiagnosticsARScreen extends StatefulWidget {
  final List<double> liveVoltages;
  final List<double> liveTemperatures;
  final bool packetAnomaly;

  const Sam2DiagnosticsARScreen({
    Key? key,
    required this.liveVoltages,
    required this.liveTemperatures,
    required this.packetAnomaly,
  }) : super(key: key);

  @override
  _Sam2DiagnosticsARScreenState createState() => _Sam2DiagnosticsARScreenState();
}

class _Sam2DiagnosticsARScreenState extends State<Sam2DiagnosticsARScreen> {
  CameraController? _cameraController;
  Interpreter? _encoderInterpreter;
  Interpreter? _decoderInterpreter;
  bool _isModelLoaded = false;
  bool _isProcessingFrame = false;
  
  // Tracking masks parsed from Qualcomm AI Hub SAM 2 output
  List<List<Offset>> _detectedCellSegments = [];
  Map<int, String> _cellStatusOverlay = {};

  @override
  void initState() {
    super.initState();
    _initializeCamera();
    _loadQualcommAIHubModels();
  }

  /// 1. Load Quantized SAM 2 Models compiled from Qualcomm AI Hub for OnePlus 15 HTP NPU
  Future<void> _loadQualcommAIHubModels() async {
    try {
      // Configure NPU delegates for Hardware acceleration (Hexagon/Adreno)
      final options = InterpreterOptions();
      
      // Use NNAPI delegate to automatically map layers to Snapdragon NPU
      options.addDelegate(NnapiDelegate(
        options: NnapiDelegateOptions(
          useNnapiCpu: false,
          acceleratorName: "qti-npu", // Force Qualcomm Snapdragon HTP NPU accelerator
        )
      ));
      
      // Load the segmented SAM 2 Image Encoder and Decoder models
      _encoderInterpreter = await Interpreter.fromAsset(
        'models/sam2_image_encoder_quantized.tflite', 
        options: options
      );
      _decoderInterpreter = await Interpreter.fromAsset(
        'models/sam2_prompt_decoder_quantized.tflite', 
        options: options
      );

      setState(() {
        _isModelLoaded = true;
      });
      print("[NPU Engine] Qualcomm AI Hub SAM 2 models loaded on Hexagon NPU.");
    } catch (e) {
      print("[NPU ERR] Failed to load Qualcomm AI Hub Models: $e. Running CPU fallback.");
      // CPU fallback configuration
      try {
        _encoderInterpreter = await Interpreter.fromAsset('models/sam2_image_encoder_quantized.tflite');
        _decoderInterpreter = await Interpreter.fromAsset('models/sam2_prompt_decoder_quantized.tflite');
        setState(() {
          _isModelLoaded = true;
        });
      } catch (err) {
        print("[CRITICAL] Could not load fallbacks: $err");
      }
    }
  }

  /// 2. Initialize Camera Feed
  Future<void> _initializeCamera() async {
    final cameras = await availableCameras();
    if (cameras.isEmpty) return;

    _cameraController = CameraController(
      cameras.first,
      ResolutionPreset.medium, // Optimized resolution to restrict inference NPU pipeline latency
      enableAudio: false,
      imageFormatGroup: ImageFormatGroup.yuv420, // Standard Android YUV frames
    );

    await _cameraController!.initialize();
    if (!mounted) return;

    // Start streaming frame frames
    _cameraController!.startImageStream((CameraImage image) {
      if (!_isModelLoaded || _isProcessingFrame) return;
      _processCameraFrame(image);
    });

    setState(() {});
  }

  /// 3. Convert image stream packet and run Qualcomm AI Hub SAM 2 NPU inference
  Future<void> _processCameraFrame(CameraImage image) async {
    _isProcessingFrame = true;

    try {
      // A. Extract and convert camera Frame buffer to normalized float array (1x3x1024x1024)
      // Input tensors required for SAM 2 encoder
      final inputBytes = _convertYUV420ToFloatBuffer(image, 1024, 1024);
      
      // B. Allocate output buffers for image embeddings (1x256x64x64)
      final encoderOutput = List.generate(
        1 * 256 * 64 * 64, 
        (index) => 0.0
      ).reshape([1, 256, 64, 64]);

      // C. Execute Image Encoder on NPU
      _encoderInterpreter!.run(inputBytes, encoderOutput);

      // D. Define point prompts corresponding to battery locations inside the screen
      // Typically battery cells map to the center regions of the camera feed
      final pointPrompts = [
        [0.35, 0.45], // Cell 1 center coordinate
        [0.45, 0.45], // Cell 2 center coordinate
        [0.55, 0.45], // Cell 3 center coordinate
        [0.65, 0.45]  // Cell 4 center coordinate
      ];

      List<List<Offset>> freshSegments = [];
      Map<int, String> freshOverlays = {};

      // E. For each target coordinates prompt, run the light-decoder on Snapdragon NPU
      for (int i = 0; i < pointPrompts.length; i++) {
        final prompt = pointPrompts[i];
        
        // Input prompt coordinates mapping (1x1x2 tensor)
        final promptInputs = [
          [ [prompt[0] * 1024.0, prompt[1] * 1024.0] ]
        ];
        
        // Input prompt labels (1 = foreground points) (1x1 tensor)
        final promptLabels = [ [1] ];

        // Output mask tensor format (1x1x256x256)
        final maskOutput = List.generate(
          1 * 1 * 256 * 256, 
          (index) => 0.0
        ).reshape([1, 1, 256, 256]);

        final decoderInputs = [
          encoderOutput,
          promptInputs,
          promptLabels
        ];

        // Execute prompt decoder on Snapdragon NPU
        _decoderInterpreter!.runForMultipleInputs(decoderInputs, {0: maskOutput});

        // F. Extract mask polygon boundary from output probability map
        final points = _extractPolygonBoundary(maskOutput[0][0], image.width, image.height, prompt);
        if (points.isNotEmpty) {
          freshSegments.add(points);
          // Map telemetry information directly to the visual segment
          double volt = widget.liveVoltages.length > i ? widget.liveVoltages[i] : 4.0;
          double temp = widget.liveTemperatures.length > i ? widget.liveTemperatures[i] : 25.0;
          freshOverlays[i] = "Cell ${i + 1}: ${volt.toStringAsFixed(2)}V | ${temp.toStringAsFixed(1)}°C";
        }
      }

      setState(() {
        _detectedCellSegments = freshSegments;
        _cellStatusOverlay = freshOverlays;
      });

    } catch (e) {
      print("[INFERENCE ERROR] SAM 2 NPU step failed: $e");
    } finally {
      // Re-enable NPU processing loop for the next frame
      _isProcessingFrame = false;
    }
  }

  /// Extracts the boundary coordinates above probability threshold (>0.5) to render custom polygon overlays
  List<Offset> _extractPolygonBoundary(List<List<double>> mask, int screenW, int screenH, List<double> prompt) {
    List<Offset> boundary = [];
    
    // Scan mask grid (256x256). To optimize UI latency, sample coordinates at step increments of 8
    for (int y = 0; y < 256; y += 8) {
      for (int x = 0; x < 256; x += 8) {
        if (mask[y][x] > 0.5) {
          // Map index coordinates to screen scale
          double scaleX = screenW / 256.0;
          double scaleY = screenH / 256.0;
          boundary.add(Offset(x * scaleX, y * scaleY));
        }
      }
    }
    return boundary;
  }

  /// Converts standard Android YUV_420 camera image plane to floating-point RGB model input buffers matches 1x3x1024x1024
  Float32List _convertYUV420ToFloatBuffer(CameraImage image, int targetW, int targetH) {
    final floatBuffer = Float32List(1 * 3 * targetW * targetH);
    final yPlane = image.planes[0].bytes;
    final yRowStride = image.planes[0].bytesPerRow;

    int outputIndex = 0;
    // Perform nearest-neighbor scaling extraction to 1024x1024
    for (int c = 0; c < 3; c++) {
      for (int y = 0; y < targetH; y++) {
        int srcY = ((y / targetH) * image.height).toInt();
        for (int x = 0; x < targetW; x++) {
          int srcX = ((x / targetW) * image.width).toInt();
          
          // Fast YUV extraction (approximating greyscale value from Luma plane for fast NPU processing)
          int yValue = yPlane[srcY * yRowStride + srcX] & 0xFF;
          floatBuffer[outputIndex++] = yValue / 255.0; // Normalize [0.0, 1.0]
        }
      }
    }
    return floatBuffer;
  }

  @override
  Widget build(BuildContext context) {
    if (_cameraController == null || !_cameraController!.value.isInitialized) {
      return const Scaffold(
        backgroundColor: Colors.black,
        body: Center(child: CircularProgressIndicator(color: Colors.cyan)),
      );
    }

    return Scaffold(
      backgroundColor: Colors.black,
      appBar: AppBar(
        title: const Text("SAM 2 Diagnostics HUD"),
        backgroundColor: Colors.black,
      ),
      body: Stack(
        fit: StackFit.expand,
        children: [
          // 1. Live Camera Preview
          CameraPreview(_cameraController!),

          // 2. Translucent Custom Painter rendering the NPU segments and Live Telemetry labels
          CustomPaint(
            painter: CellMaskPainter(
              segments: _detectedCellSegments,
              overlays: _cellStatusOverlay,
              voltages: widget.liveVoltages,
              isAnomaly: widget.packetAnomaly,
            ),
          ),
          
          // 3. User Helper Overlay
          Positioned(
            bottom: 20,
            left: 20,
            right: 20,
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
              decoration: BoxDecoration(
                color: Colors.black.withOpacity(0.85),
                borderRadius: BorderRadius.circular(10),
                border: Border.all(color: widget.packetAnomaly ? Colors.red : Colors.cyan),
              ),
              child: Text(
                widget.packetAnomaly 
                  ? "⚠️ CRITICAL STATUS: Visual Diagnostics shows thermal/voltage anomaly. Keep hands clear!"
                  : "🎯 SAM 2 NPU ACTIVE: Point camera at open battery pack to segment cells.",
                style: TextStyle(
                  color: widget.packetAnomaly ? Colors.redAccent : Colors.cyanAccent,
                  fontWeight: FontWeight.bold,
                  fontSize: 12
                ),
                textAlign: TextAlign.center,
              ),
            ),
          )
        ],
      ),
    );
  }

  @override
  void dispose() {
    _cameraController?.dispose();
    _encoderInterpreter?.close();
    _decoderInterpreter?.close();
    super.dispose();
  }
}

/// Helper drawing canvas overlays
class CellMaskPainter extends CustomPainter {
  final List<List<Offset>> segments;
  final Map<int, String> overlays;
  final List<double> voltages;
  final bool isAnomaly;

  CellMaskPainter({
    required this.segments,
    required this.overlays,
    required this.voltages,
    required this.isAnomaly
  });

  @override
  void paint(Canvas canvas, Size size) {
    for (int i = 0; i < segments.length; i++) {
      final points = segments[i];
      if (points.isEmpty) continue;

      // Extract details for color setting
      double volt = voltages.length > i ? voltages[i] : 4.0;
      
      // Determine color based on voltage health or overall anomaly
      Color segmentColor;
      if (volt < 2.5) {
        segmentColor = Colors.red.withOpacity(0.5); // Faulty cell
      } else if (volt < 3.2) {
        segmentColor = Colors.orange.withOpacity(0.4); // Low charge
      } else {
        segmentColor = Colors.green.withOpacity(0.35); // Healthy
      }

      // Draw mask polygon overlay
      final paint = Paint()
        ..color = segmentColor
        ..style = PaintingStyle.fill;
        
      final borderPaint = Paint()
        ..color = segmentColor.withOpacity(0.9)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2.0;

      final path = Path()..moveTo(points.first.dx, points.first.dy);
      for (var point in points) {
        path.lineTo(point.dx, point.dy);
      }
      path.close();

      canvas.drawPath(path, paint);
      canvas.drawPath(path, borderPaint);

      // Draw Telemetry Text overlay adjacent to the first cell coordinate point
      if (points.isNotEmpty && overlays.containsKey(i)) {
        final textSpan = TextSpan(
          text: overlays[i],
          style: const TextStyle(
            color: Colors.white,
            fontSize: 10,
            fontWeight: FontWeight.bold,
            backgroundColor: Colors.black85
          ),
        );
        final textPainter = TextPainter(
          text: textSpan,
          textDirection: TextDirection.ltr,
        );
        textPainter.layout();
        
        // Offset slightly above the segment center coordinate
        textPainter.paint(canvas, Offset(points.first.dx, points.first.dy - 12));
      }
    }
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => true;
}
