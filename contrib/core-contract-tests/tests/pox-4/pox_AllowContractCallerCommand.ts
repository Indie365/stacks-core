import { PoxCommand, Real, Stub, Wallet } from "./pox_CommandModel.ts";
import { expect } from "vitest";
import { boolCV, Cl } from "@stacks/transactions";

/**
 * The `AllowContractCallerComand` gives a `contract-caller` authorization to call stacking methods.
 * Normally, stacking methods may only be invoked by direct transactions (i.e., the tx-sender
 * issues a direct contract-call to the stacking methods).
 * By issuing an allowance, the tx-sender may call stacking methods through the allowed contract.
 *
 * There are no constraints for running this command.
 */
export class AllowContractCallerCommand implements PoxCommand {
  readonly wallet: Wallet;
  readonly allowanceTo: Wallet;
  readonly allowUntilBurnHt: number | undefined;

  /**
   * Constructs an `AllowContractCallerComand` that authorizes a `contract-caller` to call
   * stacking methods.
   *
   * @param wallet - Represents the Stacker's wallet.
   * @param allowanceTo - Represents the authorized `contract-caller` (i.e. a stacking pool)
   * @param alllowUntilBurnHt - The burn block height until the authorization is valid.
   */

  constructor(
    wallet: Wallet,
    allowanceTo: Wallet,
    allowUntilBurnHt: number | undefined,
  ) {
    this.wallet = wallet;
    this.allowanceTo = allowanceTo;
    this.allowUntilBurnHt = allowUntilBurnHt;
  }

  check(): boolean {
    // There are no constraints for running this command.
    return true;
  }

  run(model: Stub, real: Real): void {
    // Arrange
    const untilBurnHtOptionalCv = this.allowUntilBurnHt === undefined
      ? Cl.none()
      : Cl.some(Cl.uint(this.allowUntilBurnHt));

    // Act
    const allowContractCaller = real.network.callPublicFn(
      "ST000000000000000000002AMW42H.pox-4",
      "allow-contract-caller",
      [
        // (caller principal)
        Cl.principal(this.allowanceTo.stxAddress),
        // (until-burn-ht (optional uint))
        untilBurnHtOptionalCv,
      ],
      this.wallet.stxAddress,
    );

    // Assert
    expect(allowContractCaller.result).toBeOk(boolCV(true));

    // Get the wallets involved from the model and update it with the new state.
    const wallet = model.wallets.get(this.wallet.stxAddress)!;
    const callerToAllow = model.wallets.get(this.allowanceTo.stxAddress)!;
    // Update model so that we know this wallet has authorized a contract-caller. 

    wallet.allowedContractCaller = this.allowanceTo.stxAddress;
    callerToAllow.callerAllowedBy.push(wallet.stxAddress);

    // Log to console for debugging purposes. This is not necessary for the
    // test to pass but it is useful for debugging and eyeballing the test.
    console.info(
      `✓ ${
        this.wallet.label.padStart(
          8,
          " ",
        )
      } ${
        "allow-contract-caller".padStart(
          34,
          " ",
        )
      } ${this.allowanceTo.label.padStart(12, " ")} ${"until".padStart(53)} ${
        (this.allowUntilBurnHt || "none").toString().padStart(17)
      }`,
    );
  }

  toString() {
    // fast-check will call toString() in case of errors, e.g. property failed.
    // It will then make a minimal counterexample, a process called 'shrinking'
    // https://github.com/dubzzz/fast-check/issues/2864#issuecomment-1098002642
    return `${this.wallet.stxAddress} allow-contract-caller ${this.allowanceTo.stxAddress} until burn ht ${this.allowUntilBurnHt}`;
  }
}
