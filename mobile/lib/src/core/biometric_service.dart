import 'package:flutter/services.dart';
import 'package:local_auth/local_auth.dart';

/// Thin wrapper over [LocalAuthentication] for the biometric app-lock.
///
/// All calls degrade gracefully: a platform error (no hardware, not enrolled,
/// too many attempts, user cancel) resolves to `false` rather than throwing, so
/// the caller never traps the user behind a lock they can't satisfy.
class BiometricService {
  BiometricService([LocalAuthentication? auth])
      : _auth = auth ?? LocalAuthentication();

  final LocalAuthentication _auth;

  /// True only when the device supports biometrics AND the user has enrolled at
  /// least one (Face ID / fingerprint). Used to decide whether the lock can be
  /// offered/applied.
  Future<bool> isAvailable() async {
    try {
      if (!await _auth.isDeviceSupported()) return false;
      if (!await _auth.canCheckBiometrics) return false;
      final enrolled = await _auth.getAvailableBiometrics();
      return enrolled.isNotEmpty;
    } on PlatformException {
      return false;
    }
  }

  /// Prompt the user. Returns true on success. Device passcode is allowed as a
  /// fallback (`biometricOnly: false`) so a transient sensor failure doesn't
  /// strand the user.
  Future<bool> authenticate({required String reason}) async {
    try {
      return await _auth.authenticate(
        localizedReason: reason,
        options: const AuthenticationOptions(
          stickyAuth: true,
          biometricOnly: false,
        ),
      );
    } on PlatformException {
      return false;
    }
  }
}
