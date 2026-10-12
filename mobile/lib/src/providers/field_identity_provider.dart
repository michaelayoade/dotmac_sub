import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../repositories/field_identity_repository.dart';
import 'auth_controller.dart';

final fieldIdentityRepositoryProvider = Provider<FieldIdentityRepository>(
  (ref) => FieldIdentityRepository(ref.watch(apiClientProvider).dio),
);
