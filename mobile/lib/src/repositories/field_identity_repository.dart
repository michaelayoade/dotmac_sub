import 'dart:typed_data';
import 'package:dio/dio.dart';
import '../core/http.dart';
import '../models/field_identity.dart';

class FieldIdentityRepository {
  FieldIdentityRepository(this.dio);
  final Dio dio;
  String _path(String workOrderId) {
    if (!isNativeIdentityId(workOrderId)) {
      throw const FormatException('Native visit identity required');
    }
    return '/me/work-orders/$workOrderId/field-identity';
  }

  Future<FieldIdentityResult> card(String workOrderId) async {
    final data = await guard(
      () => dio.get(
        _path(workOrderId),
        options: Options(extra: {'requireFresh': true}),
      ),
    );
    return FieldIdentityResult.fromJson(
      (data as Map).cast<String, dynamic>(),
      expectedWorkOrderId: workOrderId,
    );
  }

  Future<Uint8List> portrait(
    String workOrderId,
    ApprovedFieldIdentity identity,
  ) async {
    final data = await guard(
      () => dio.get(
        '${_path(workOrderId)}/portrait',
        queryParameters: {
          'assignment_generation': identity.assignmentGeneration,
          'portrait_revision': identity.portraitRevision,
        },
        options: Options(
          extra: {'requireFresh': true},
          responseType: ResponseType.bytes,
        ),
      ),
    );
    if (data is! List<int> || data.isEmpty) {
      throw const FormatException('Portrait unavailable');
    }
    return Uint8List.fromList(data);
  }
}
