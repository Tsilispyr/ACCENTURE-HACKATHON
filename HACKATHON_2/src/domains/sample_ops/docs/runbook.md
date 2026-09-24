# Operations Runbook

## Triage
Classify every incident before acting. Use the service metrics, not the
reporter's description, to decide severity. A description is a claim; a metric
is evidence. Tier 1 services (payment-service, identity-service) are always at
least medium severity.

## Connection Pool Exhaustion
When connections reach the pool maximum, the service reports connection
timeouts even though CPU is normal. That combination - timeouts with healthy
CPU - is the signature of pool exhaustion rather than load.

Raise the pool before restarting. Scaling the pool is additive and reversible;
a restart drops every in-flight transaction. Restarting first destroys the
evidence that would have confirmed the diagnosis.

## Restart Policy
Restarting a tier 1 service requires human approval, because it interrupts
in-flight transactions that cannot be replayed. Tier 2 and 3 services may be
restarted once without approval, twice only after escalation.

Never restart a service that has already been restarted within the last ten
minutes; a restart loop looks like recovery for about thirty seconds.

## Verification
A remediation receipt is not proof of recovery. The actor that made a change
cannot also be the judge of whether it worked. Confirm recovery with an
independent health check and a fresh metric read.

If verification fails twice, escalate to the on-call engineer rather than
retrying a third time.

## Escalation
Escalate when: verification has failed twice, the incident affects a tier 1
service for more than thirty minutes, or the remediation required is outside
the approved runbook.
