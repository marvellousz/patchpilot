Title: Checkout crashes with TypeError when the card gateway times out

**What happens**

During the gateway slowdown this morning, checkout requests started failing with a 500 instead of
retrying. Logs from `PaymentService`:

```
2026-11-06 10:21:33 ERROR checkout - unhandled exception processing order ord_8812
Traceback (most recent call last):
  File "payments/service.py", line 17, in process
    if resp["status"] == "SUCCESS":
TypeError: 'NoneType' object is not subscriptable
```

**Expected**

A gateway timeout should be treated like a `PENDING` reply: try again, and if the gateway still
hasn't confirmed after `max_retries`, raise `PaymentError` so checkout can show the
"payment could not be confirmed" message.

**Notes**

`HttpGateway.charge` returns `None` when the gateway doesn't answer within its timeout.
