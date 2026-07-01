import 'package:dotmac_portal/src/models/reseller_crm.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('Reseller CRM models', () {
    test('ResellerQuote wraps Quote + carries the account', () {
      final rq = ResellerQuote.fromJson({
        'account_id': 'a1',
        'account_name': 'Acme Ltd',
        'id': 'q1',
        'status': 'draft',
        'total': '75000.00',
        'deposit_amount': '37500.00',
        'feasibility': {'coverage': 'covered'},
      });
      expect(rq.accountId, 'a1');
      expect(rq.accountName, 'Acme Ltd');
      expect(rq.quote.id, 'q1');
      expect(rq.quote.feasibility.isCovered, isTrue);
    });

    test('ResellerProject + WorkOrder parse compact rows', () {
      final p = ResellerProject.fromJson({
        'account_id': 'a1',
        'account_name': 'Acme',
        'id': 'p1',
        'name': 'Fiber install',
        'status': 'open',
        'progress_pct': 40,
        'current_stage': 'Cabling',
      });
      expect(p.progressPct, 40);
      expect(p.currentStage, 'Cabling');

      final w = ResellerWorkOrder.fromJson({
        'account_id': 'a1',
        'id': 'w1',
        'title': 'Repair',
        'status': 'dispatched',
        'technician_name': 'Ada',
      });
      expect(w.title, 'Repair');
      expect(w.technicianName, 'Ada');
    });

    test('parseResellerList reads the envelope + skips non-maps', () {
      final list = parseResellerList(
        {
          'quotes': [
            {
              'account_id': 'a1',
              'id': 'q1',
              'status': 'draft',
              'feasibility': {}
            },
            'garbage',
          ],
          'total': 1,
        },
        'quotes',
        ResellerQuote.fromJson,
      );
      expect(list.length, 1);
      expect(list.single.quote.id, 'q1');
    });
  });
}
