import { PoxCommand, Real, Stub, Wallet } from "./pox_CommandModel.ts";
import { poxAddressToTuple } from "@stacks/stacking";
import { assert, expect } from "vitest";
import { Cl, ClarityType, isClarityType } from "@stacks/transactions";

/**
 * The `DelegateStackStxCommand` locks STX for stacking within PoX-4 on behalf of a delegator.
 * This operation allows the `operator` to stack the `stacker`'s STX.
 *
 * Constraints for running this command include:
 * - A minimum threshold of uSTX must be met, determined by the
 *  `get-stacking-minimum` function at the time of this call.
 * - The Stacker cannot currently be engaged in another stacking
 *   operation.
 * - The Stacker has to currently be delegating to the Operator.
 * - The stacked STX amount should be less than or equal to the
 *   delegated amount.
 * - The stacked uSTX amount should be less than or equal to the
 *   Stacker's balance.
 * - The stacked uSTX amount should be greater than or equal to the
 *   minimum threshold of uSTX.
 * - The Operator has to currently be delegated by the Stacker.
 * - The Period has to fit the last delegation burn block height.
 */
export class DelegateStackStxCommand implements PoxCommand {
  readonly operator: Wallet;
  readonly stacker: Wallet;
  readonly startBurnHt: number;
  readonly period: number;
  readonly amountUstx: bigint;
  readonly unlockBurnHt: number;

  /**
   * Constructs a `DelegateStackStxCommand` to lock uSTX as a Pool Operator
   * on behalf of a Stacker.
   *
   * @param operator - Represents the Pool Operator's wallet.
   * @param stacker - Represents the STacker's wallet.
   * @param startBurnHt - A burn height inside the current reward cycle.
   * @param period - Number of reward cycles to lock uSTX.
   * @param amountUstx - The uSTX amount stacked by the Operator on behalf
   *                     of the Stacker
   * @param unlockBurnHt - The burn height at which the uSTX is unlocked.
   */
  constructor(
    operator: Wallet,
    stacker: Wallet,
    startBurnHt: number,
    period: number,
    amountUstx: bigint,
    unlockBurnHt: number,
  ) {
    this.operator = operator;
    this.stacker = stacker;
    this.startBurnHt = startBurnHt;
    this.period = period;
    this.amountUstx = amountUstx;
    this.unlockBurnHt = unlockBurnHt;
  }

  check(model: Readonly<Stub>): boolean {
    // Constraints for running this command include:
    // - A minimum threshold of uSTX must be met, determined by the
    //   `get-stacking-minimum` function at the time of this call.
    // - The Stacker cannot currently be engaged in another stacking
    //   operation.
    // - The Stacker has to currently be delegating to the Operator.
    // - The stacked uSTX amount should be less than or equal to the
    //   delegated amount.
    // - The stacked uSTX amount should be less than or equal to the
    //   Stacker's balance.
    // - The stacked uSTX amount should be greater than or equal to the
    //   minimum threshold of uSTX.
    // - The Operator has to currently be delegated by the Stacker.
    // - The Period has to fit the last delegation burn block height.

    const operatorWallet = model.wallets.get(this.operator.stxAddress)!;
    const stackerWallet = model.wallets.get(this.stacker.stxAddress)!;

    return (
      model.stackingMinimum > 0 &&
      !stackerWallet.isStacking &&
      stackerWallet.hasDelegated &&
      stackerWallet.delegatedMaxAmount >= Number(this.amountUstx) &&
      Number(this.amountUstx) <= stackerWallet.ustxBalance &&
      Number(this.amountUstx) >= model.stackingMinimum &&
      operatorWallet.hasPoolMembers.includes(stackerWallet.stxAddress) &&
      this.unlockBurnHt <= stackerWallet.delegatedUntilBurnHt
    );
  }

  run(model: Stub, real: Real): void {
    // Act
    const delegateStackStx = real.network.callPublicFn(
      "ST000000000000000000002AMW42H.pox-4",
      "delegate-stack-stx",
      [
        // (stacker principal)
        Cl.principal(this.stacker.stxAddress),
        // (amount-ustx uint)
        Cl.uint(this.amountUstx),
        // (pox-addr { version: (buff 1), hashbytes: (buff 32) })
        poxAddressToTuple(this.operator.btcAddress),
        // (start-burn-ht uint)
        Cl.uint(this.startBurnHt),
        // (lock-period uint)
        Cl.uint(this.period)
      ],
      this.operator.stxAddress,
    );
    const { result: rewardCycle } = real.network.callReadOnlyFn(
      "ST000000000000000000002AMW42H.pox-4",
      "burn-height-to-reward-cycle",
      [Cl.uint(real.network.blockHeight)],
      this.operator.stxAddress,
    );
    assert(isClarityType(rewardCycle, ClarityType.UInt));

    const { result: unlockBurnHeight } = real.network.callReadOnlyFn(
      "ST000000000000000000002AMW42H.pox-4",
      "reward-cycle-to-burn-height",
      [Cl.uint(Number(rewardCycle.value) + this.period + 1)],
      this.operator.stxAddress,
    );
    assert(isClarityType(unlockBurnHeight, ClarityType.UInt));

    // Assert
    expect(delegateStackStx.result).toBeOk(
      Cl.tuple({
        stacker: Cl.principal(this.stacker.stxAddress),
        "lock-amount": Cl.uint(this.amountUstx),
        "unlock-burn-height": Cl.uint(Number(unlockBurnHeight.value)),
      }),
    );

    // Get the Stacker's wallet from the model and update it with the new state.
    const stackerWallet = model.wallets.get(this.stacker.stxAddress)!;
    // Update model so that we know this wallet is stacking. This is important
    // in order to prevent the test from stacking multiple times with the same
    // address.
    stackerWallet.isStacking = true;
    // Update locked, unlocked, and unlock-height fields in the model.
    stackerWallet.amountLocked = Number(this.amountUstx);
    stackerWallet.unlockHeight = Number(unlockBurnHeight.value);
    stackerWallet.amountUnlocked -= Number(this.amountUstx);

    // Log to console for debugging purposes. This is not necessary for the
    // test to pass but it is useful for debugging and eyeballing the test.
    console.info(
      `✓ ${this.operator.label.padStart(8, " ")} Ӿ ${this.stacker.label.padStart(8, " ")} ${
        "delegate-stack-stx".padStart(23, " ")
      } ${"lock-amount".padStart(12, " ")} ${
        this.amountUstx.toString().padStart(15, " ")
      } ${"until".padStart(37)} ${this.stacker.unlockHeight.toString().padStart(17)}`,
    );
  }

  toString() {
    // fast-check will call toString() in case of errors, e.g. property failed.
    // It will then make a minimal counterexample, a process called 'shrinking'
    // https://github.com/dubzzz/fast-check/issues/2864#issuecomment-1098002642
    return `${this.operator.label} delegate-stack-stx period ${this.period}`;
  }
}
