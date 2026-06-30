"""
Dummy-Adapter fuer den Turntable (Stepper-Motor, ADR-002 Abschnitt 10).

Gibt auf der Konsole aus, in welche Richtung der Turntable drehen
wuerde, statt echte GPIO-Pulse zu senden. Implementiert dieselbe
Trägheitsschwelle (ROTATION_THRESHOLD_DEGREES) wie die spaetere echte
Implementierung, damit das Verhalten bereits jetzt beobachtbar ist.

ECHTE IMPLEMENTIERUNG (spaeter): 28BYJ-48 + ULN2003 ueber gpiozero,
siehe ADR-002 Abschnitt 10.
"""

from domain.policies import ROTATION_THRESHOLD_DEGREES


class DummyTurntableAdapter:
    def __init__(self) -> None:
        self.current_heading_degrees = 0.0

    def rotate_towards(self, target_angle_degrees: float) -> None:
        delta = target_angle_degrees - self.current_heading_degrees
        if abs(delta) > ROTATION_THRESHOLD_DEGREES:
            direction = "rechts" if delta > 0 else "links"
            print(f"  [DummyTurntable] 🔄 Drehe {direction} um {abs(delta):.0f}° "
                  f"(Ziel: {target_angle_degrees:.0f}°)")
            self.current_heading_degrees = target_angle_degrees
        else:
            print(f"  [DummyTurntable] Bleibe stehen (Delta {delta:.0f}° "
                  f"unter Schwelle {ROTATION_THRESHOLD_DEGREES}°)")
