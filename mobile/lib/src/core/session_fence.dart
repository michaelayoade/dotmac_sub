/// The in-memory half of session fencing.
///
/// Every signed-in session gets a *generation*: a number that only ever goes
/// up on this device (it is persisted alongside the credentials, so it survives
/// a restart and is never reused). Work started under one session carries that
/// session's generation with it; when the work finally completes, it is only
/// allowed to take effect if the generation it carries is still the current one.
///
/// This is what makes the nastiest race in the app safe. A 401 kicks off a token
/// refresh; while that refresh is in flight the user taps "sign out". Without a
/// fence the refresh completes a moment later and writes a *fresh, valid* token
/// pair back into secure storage — the user is signed out on screen but the
/// device is holding live credentials for the account they just left. With the
/// fence, sign-out closes the generation first, and the returning refresh finds
/// itself stale and discards its result instead of persisting it.
///
/// The fence is the fast, synchronous check. It is not the only one: the
/// durable authority is the stored credential record, and every write into it
/// is additionally gated on the generation it was started under (see
/// `TokenStorage.renewSession`). A fence that has been lost — a process
/// restart, a rebuilt provider container — therefore cannot open a hole; at
/// worst it falls back to the storage-level check.
class SessionFence {
  /// 0 means "no session". A generation is never 0, so anything carrying 0 is
  /// refused by [holds].
  int _generation = 0;

  int get current => _generation;

  bool get isOpen => _generation > 0;

  /// Begin (or resume) a session at [generation]. Callers get the number from
  /// the persisted credential record, which owns the monotonic counter.
  void open(int generation) {
    if (generation <= 0) {
      throw ArgumentError.value(generation, 'generation', 'must be positive');
    }
    _generation = generation;
  }

  /// End the current session. Everything in flight is now stale.
  void close() {
    _generation = 0;
  }

  /// Whether [generation] is the session that is still running. Deliberately
  /// takes an [Object?] so callers can pass a value straight out of a Dio
  /// `RequestOptions.extra` bag without a cast dance; anything that is not a
  /// positive int matching the current generation is stale.
  bool holds(Object? generation) =>
      _generation > 0 && generation is int && generation == _generation;
}
