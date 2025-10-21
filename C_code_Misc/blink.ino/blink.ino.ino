// LED_BUILTIN in connected to pin 25 of the RP2040 chip.
// It controls the on board LED, at the top-left corner.

void setup() {
  pinMode(15, OUTPUT);
}

void loop() {
  digitalWrite(15, HIGH);
  delay(500);
  digitalWrite(15, LOW);
  delay(500);
}