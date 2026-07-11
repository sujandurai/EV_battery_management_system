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
  
  // Set pixels corresponding to character 'S'
  for (int r = 0; r < 8; r++) {
    for (int c = 0; c < 13; c++) {
      if (character_S[r][c] == 1) {
        matrix.set(r, c, 1);
      } else {
        matrix.set(r, c, 0);
      }
    }
  }
}

void loop() {
  // Normal operational delay - 'S' remains stable
  delay(1000);
}
