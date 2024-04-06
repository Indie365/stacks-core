import {
  logCommand,
  PoxCommand,
  Real,
  Stub,
  Wallet,
} from "./pox_CommandModel.ts";
import { Pox4SignatureTopic, poxAddressToTuple } from "@stacks/stacking";
import { expect } from "vitest";
import { Cl } from "@stacks/transactions";
import { bufferFromHex } from "@stacks/transactions/dist/cl";

/**
 * The `StackAggregationCommitIndexedSigCommand` allows an operator to
 * commit partially stacked STX & to allocate a new PoX reward address
 * slot.
 * This allows a stacker to lock fewer STX than the minimal threshold
 * in multiple transactions, so long as:
 *  1. The pox-addr is the same.
 *  2. The "commit" transaction is called _before_ the PoX anchor block.
 *
 * This command calls `stack-aggregation-commit-indexed` using a
 * `signature`.
 *
 * Constraints for running this command include:
 * - The Operator must have locked STX on behalf of at least one stacker.
 * - The total amount previously locked by the Operator on behalf of the
 *   stackers has to be greater than the uSTX threshold.
 */
export class StackAggregationCommitIndexedSigCommand implements PoxCommand {
  readonly operator: Wallet;
  readonly authId: number;
  readonly currentCycle: number;

  /**
   * Constructs a `StackAggregationCommitIndexedSigCommand` to lock uSTX 
   * for stacking.
   *
   * @param operator - Represents the `Operator`'s wallet.
   * @param authId - Unique `auth-id` for the authorization.
   * @param currentCycle - The current reward cycle.
   */
  constructor(operator: Wallet, authId: number, currentCycle: number) {
    this.operator = operator;
    this.authId = authId;
    this.currentCycle = currentCycle;
  }

  check(model: Readonly<Stub>): boolean {
    // Constraints for running this command include:
    // - The Operator must have locked STX on behalf of at least one stacker.
    // - The total amount previously locked by the Operator on behalf of the
    //   stackers has to be greater than the uSTX threshold.

    return (
      this.operator.lockedAddresses.length > 0 &&
      this.operator.amountToCommit >= model.stackingMinimum
    );
  }

  run(model: Stub, real: Real): void {
    model.trackCommandRun(this.constructor.name);

    const committedAmount = this.operator.amountToCommit;

    const signerSig = this.operator.stackingClient.signPoxSignature({
      // The signer key being authorized.
      signerPrivateKey: this.operator.signerPrvKey,
      // The reward cycle for which the authorization is valid.
      // For stack-stx and stack-extend, this refers to the reward cycle
      // where the transaction is confirmed. For stack-aggregation-commit,
      // this refers to the reward cycle argument in that function.
      rewardCycle: this.currentCycle + 1,
      // For stack-stx, this refers to lock-period. For stack-extend,
      // this refers to extend-count. For stack-aggregation-commit, this is
      // u1.
      period: 1,
      // A string representing the function where this authorization is valid.
      // Either stack-stx, stack-extend, stack-increase or agg-commit.
      topic: Pox4SignatureTopic.AggregateCommit,
      // The PoX address that can be used with this signer key.
      poxAddress: this.operator.btcAddress,
      // The unique auth-id for this authorization.
      authId: this.authId,
      // The maximum amount of uSTX that can be used (per tx) with this signer
      // key.
      maxAmount: committedAmount,
    });

    // Act
    const stackAggregationCommitIndexed = real.network.callPublicFn(
      "ST000000000000000000002AMW42H.pox-4",
      "stack-aggregation-commit-indexed",
      [
        // (pox-addr (tuple (version (buff 1)) (hashbytes (buff 32))))
        poxAddressToTuple(this.operator.btcAddress),
        // (reward-cycle uint)
        Cl.uint(this.currentCycle + 1),
        // (signer-sig (optional (buff 65)))
        Cl.some(bufferFromHex(signerSig)),
        // (signer-key (buff 33))
        Cl.bufferFromHex(this.operator.signerPubKey),
        // (max-amount uint)
        Cl.uint(committedAmount),
        // (auth-id uint)
        Cl.uint(this.authId),
      ],
      this.operator.stxAddress,
    );

    // Assert
    expect(stackAggregationCommitIndexed.result).toBeOk(
      Cl.uint(model.nextRewardSetIndex),
    );

    // Update the model
    const operatorWallet = model.wallets.get(this.operator.stxAddress)!;
    operatorWallet.amountToCommit -= committedAmount;
    operatorWallet.committedRewCycleIndexes.push(model.nextRewardSetIndex);
    model.nextRewardSetIndex++;

    // Log to console for debugging purposes. This is not necessary for the
    // test to pass but it is useful for debugging and eyeballing the test.
    logCommand(
      `✓ ${this.operator.label}`,
      "stack-agg-commit-indexed",
      "amount committed",
      committedAmount.toString(),
      "signature",
    );
  }

  toString() {
    // fast-check will call toString() in case of errors, e.g. property failed.
    // It will then make a minimal counterexample, a process called 'shrinking'
    // https://github.com/dubzzz/fast-check/issues/2864#issuecomment-1098002642
    return `${this.operator.label} stack-aggregation-commit-indexed auth-id ${this.authId} for reward cycle ${this.currentCycle}`;
  }
}
