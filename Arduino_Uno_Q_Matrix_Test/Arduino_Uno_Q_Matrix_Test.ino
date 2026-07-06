#include "Arduino_LED_Matrix.h"

ArduinoLEDMatrix matrix;

// 'S' Character Bitmask (8 Rows x 13 Columns)
const uint8_t character_S[8][13] = {
  {0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0},
  {0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0},
  {0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
  {0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0},
  {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0},
  {0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0},
  {0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0},
  {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0}
};

void setup() {
  // Initialize the built-in 8x13 blue LED matrix
  matrix.begin();
}

void loop() {
  // Load the 8x13 pixel array onto the matrix
  matrix.loadPixels((uint8_t*)character_S, sizeof(character_S));
  delay(1000);
}
