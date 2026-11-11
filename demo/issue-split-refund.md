Title: Split refunds don't add up to the refunded amount

**What happens**

Support refunded order ord_5521 ($10.00) in 3 installments and the customer only got $9.99 back.
`split_refund(Decimal("10.00"), 3)` returns `[3.33, 3.33, 3.33]`.

We've also seen the opposite: a $20.00 refund over 3 installments paid out $20.01, which is more
than the customer paid.

**Expected**

The installments should always add up to exactly the refunded amount, e.g. `3.33, 3.33, 3.34`
for $10.00.
