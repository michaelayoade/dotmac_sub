import 'package:dio/dio.dart';

import 'api_exception.dart';

/// Runs a Dio call, normalises transport errors, and turns any 4xx body into
/// an [ApiException]. Returns the response data on success (2xx/3xx).
Future<dynamic> guard(Future<Response> Function() call) async {
  late final Response res;
  try {
    res = await call();
  } on DioException catch (e) {
    throw ApiException.fromDio(e);
  }

  final status = res.statusCode ?? 0;
  if (status >= 400) {
    // Reuse the same parsing the interceptor uses for error bodies.
    throw ApiException.fromDio(
      DioException(
        requestOptions: res.requestOptions,
        response: res,
        type: DioExceptionType.badResponse,
      ),
    );
  }
  return res.data;
}
