// Copyright (C) 2013-2020 Blockstack PBC, a public benefit corporation
// Copyright (C) 2020 Stacks Open Internet Foundation
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program.  If not, see <http://www.gnu.org/licenses/>.

use chainstate::stacks::index::storage::TrieFileStorage;
use clarity_vm::clarity::ClarityInstance;
use util::hash::hex_bytes;
use vm::contexts::Environment;
use vm::contexts::{AssetMap, AssetMapEntry, GlobalContext, OwnedEnvironment};
use vm::contracts::Contract;
use vm::costs::ExecutionCost;
use vm::database::ClarityDatabase;
use vm::errors::{CheckErrors, Error, RuntimeErrorType};
use vm::execute as vm_execute;
use vm::functions::NativeFunctions;
use vm::representations::SymbolicExpression;
use vm::tests::costs::get_simple_test;
use vm::tests::{execute, symbols_from_values, with_marfed_environment, with_memory_environment};
use vm::types::{
    AssetIdentifier, OptionalData, PrincipalData, QualifiedContractIdentifier, ResponseData, Value,
};

use vm::tests::{
    execute, symbols_from_values, with_marfed_environment, with_memory_environment,
    TEST_BURN_STATE_DB, TEST_HEADER_DB,
};
use vm::types::{AssetIdentifier, PrincipalData, QualifiedContractIdentifier, ResponseData, Value};

use crate::clarity_vm::clarity::ClarityConnection;
use crate::clarity_vm::database::marf::MarfedKV;
use crate::core::StacksEpochId;
use crate::types::chainstate::{BlockHeaderHash, StacksBlockId};
use crate::types::proof::ClarityMarfTrieId;
use crate::{clarity_vm::database::marf::MarfedKV, vm::database::NULL_BURN_STATE_DB_2_1};

pub fn test_tracked_costs(prog: &str, use_mainnet: bool, epoch: StacksEpochId) -> ExecutionCost {
    let marf = MarfedKV::temporary();
    let mut clarity_instance = ClarityInstance::new(use_mainnet, marf);

    let p1 = execute("'SZ2J6ZY48GV1EZ5V2V5RB9MP66SW86PYKKQ9H6DPR");

    let p1_principal = match p1 {
        Value::Principal(PrincipalData::Standard(ref data)) => data.clone(),
        _ => panic!(),
    };

    let contract_trait = "(define-trait trait-1 (
                            (foo-exec (int) (response int int))
                          ))";
    let contract_other = "(impl-trait .contract-trait.trait-1)
                          (define-map map-foo { a: int } { b: int })
                          (define-public (foo-exec (a int)) (ok 1))";

    let contract_self = format!(
        "(define-map map-foo {{ a: int }} {{ b: int }})
        (define-non-fungible-token nft-foo int)
        (define-fungible-token ft-foo)
        (define-data-var var-foo int 0)
        (define-constant tuple-foo (tuple (a 1)))
        (define-constant list-foo (list true))
        (define-constant list-bar (list 1))
        (define-constant str-foo \"foobar\")
        (use-trait trait-1 .contract-trait.trait-1)
        (define-public (execute (contract <trait-1>)) (ok {}))",
        prog
    );

    let self_contract_id = QualifiedContractIdentifier::new(p1_principal.clone(), "self".into());
    let other_contract_id =
        QualifiedContractIdentifier::new(p1_principal.clone(), "contract-other".into());
    let trait_contract_id =
        QualifiedContractIdentifier::new(p1_principal.clone(), "contract-trait".into());

    clarity_instance
        .begin_test_genesis_block(
            &StacksBlockId::sentinel(),
            &StacksBlockId([0 as u8; 32]),
            &TEST_HEADER_DB,
            &TEST_BURN_STATE_DB,
        )
        .commit_block();

    {
        let mut conn = clarity_instance.begin_block(
            &StacksBlockId([0 as u8; 32]),
            &StacksBlockId([1 as u8; 32]),
            &TEST_HEADER_DB,
            &TEST_BURN_STATE_DB,
        );

        if epoch == StacksEpochId::Epoch2_05 {
            conn.initialize_epoch_2_05().unwrap();
        }

        conn.commit_block();
    }

    {
        let mut conn = clarity_instance.begin_block(
            &StacksBlockId([1 as u8; 32]),
            &StacksBlockId([2 as u8; 32]),
            &TEST_HEADER_DB,
            &TEST_BURN_STATE_DB,
        );

        assert_eq!(
            conn.with_clarity_db_readonly(|db| db.get_clarity_epoch_version()),
            epoch
        );

        conn.as_transaction(|conn| {
            let (ct_ast, ct_analysis) = conn
                .analyze_smart_contract(&trait_contract_id, contract_trait)
                .unwrap();
            conn.initialize_smart_contract(
                &trait_contract_id,
                &ct_ast,
                contract_trait,
                None,
                |_, _| false,
            )
            .unwrap();
            conn.save_analysis(&trait_contract_id, &ct_analysis)
                .unwrap();
        });

        conn.commit_block();
    }

    {
        let mut conn = clarity_instance.begin_block(
            &StacksBlockId([2 as u8; 32]),
            &StacksBlockId([3 as u8; 32]),
            &TEST_HEADER_DB,
            &TEST_BURN_STATE_DB,
        );

        conn.as_transaction(|conn| {
            conn.with_clarity_db(|db| {
                db.set_clarity_epoch_version(crate::core::StacksEpochId::Epoch21);
                Ok(())
            })
        })
        .unwrap();

        conn.as_transaction(|conn| {
            let (ct_ast, ct_analysis) = conn
                .analyze_smart_contract(&other_contract_id, contract_other)
                .unwrap();
            conn.initialize_smart_contract(
                &other_contract_id,
                &ct_ast,
                contract_other,
                None,
                |_, _| false,
            )
            .unwrap();
            conn.save_analysis(&other_contract_id, &ct_analysis)
                .unwrap();
        });

        conn.commit_block();
    }

    {
        let mut conn = clarity_instance.begin_block(
            &StacksBlockId([3 as u8; 32]),
            &StacksBlockId([4 as u8; 32]),
            &TEST_HEADER_DB,
            &TEST_BURN_STATE_DB,
        );

        conn.as_transaction(|conn| {
            let (ct_ast, ct_analysis) = conn
                .analyze_smart_contract(&self_contract_id, &contract_self)
                .unwrap();
            conn.initialize_smart_contract(
                &self_contract_id,
                &ct_ast,
                &contract_self,
                None,
                |_, _| false,
            )
            .unwrap();
            conn.save_analysis(&self_contract_id, &ct_analysis).unwrap();
        });

        conn.commit_block().get_total()
    }
}

fn test_all(use_mainnet: bool) {
    let baseline = test_tracked_costs("1", use_mainnet, StacksEpochId::Epoch20);

    for f in NativeFunctions::ALL.iter() {
        let test = get_simple_test(f);
        let cost = test_tracked_costs(test, use_mainnet, StacksEpochId::Epoch20);
        assert!(cost.exceeds(&baseline));
    }
}

#[test]
fn test_all_mainnet() {
    test_all(true)
}

#[test]
fn test_all_testnet() {
    test_all(false)
}

fn epoch_205_test_all(use_mainnet: bool) {
    let baseline = test_tracked_costs("1", use_mainnet, StacksEpochId::Epoch2_05);

    for f in NativeFunctions::ALL.iter() {
        let test = get_simple_test(f);
        let cost = test_tracked_costs(test, use_mainnet, StacksEpochId::Epoch2_05);
        assert!(cost.exceeds(&baseline));
    }
}

#[test]
fn epoch_205_test_all_mainnet() {
    epoch_205_test_all(true)
}

#[test]
fn epoch_205_test_all_testnet() {
    epoch_205_test_all(false)
}
