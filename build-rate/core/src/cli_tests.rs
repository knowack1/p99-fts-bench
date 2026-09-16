use super::*;

fn levels(raw: &str) -> Result<Vec<usize>, String> {
    Levels::from_str(raw).map(|parsed| parsed.0)
}

fn rates(raw: &str) -> Result<Vec<u64>, String> {
    Rates::from_str(raw).map(|parsed| parsed.0)
}

#[test]
fn a_ladder_is_comma_separated_and_keeps_its_order() {
    assert_eq!(levels("4,8,16").unwrap(), vec![4, 8, 16]);
    assert_eq!(rates("20000,40000").unwrap(), vec![20_000, 40_000]);
}

#[test]
fn surrounding_space_and_trailing_commas_are_tolerated() {
    assert_eq!(levels(" 4 , 8 ,").unwrap(), vec![4, 8]);
    assert_eq!(rates("20000, 40000,").unwrap(), vec![20_000, 40_000]);
}

#[test]
fn repeats_are_kept_on_both_ladders() {
    assert_eq!(levels("8,8,16").unwrap(), vec![8, 8, 16]);
    assert_eq!(rates("20000,20000").unwrap(), vec![20_000, 20_000]);
}

#[test]
fn an_empty_ladder_names_the_flag_that_was_given_no_steps() {
    assert_eq!(
        levels(",,").unwrap_err(),
        "--concurrency needs at least one level"
    );
    assert_eq!(
        rates(" ").unwrap_err(),
        "--target-rate needs at least one level"
    );
}

#[test]
fn a_bad_step_names_what_that_ladders_step_is_called() {
    assert_eq!(
        levels("4,0").unwrap_err(),
        "concurrency must be >= 1, got 0"
    );
    assert_eq!(
        rates("20000,0").unwrap_err(),
        "target rate must be >= 1, got 0"
    );
}

#[test]
fn a_step_that_is_not_an_integer_is_quoted_back() {
    assert_eq!(levels("4,many").unwrap_err(), "not an integer: \"many\"");
    assert_eq!(rates("fast").unwrap_err(), "not an integer: \"fast\"");
}

#[test]
fn a_ladder_round_trips_through_display_for_the_csv_header() {
    assert_eq!(Levels::from_str("4,8,16").unwrap().to_string(), "4,8,16");
    assert_eq!(
        Rates::from_str("20000,40000").unwrap().to_string(),
        "20000,40000"
    );
}

#[test]
fn path_setting_says_off_rather_than_leaving_a_header_blank() {
    assert_eq!(path_setting(None), OFF);
    assert_eq!(
        path_setting(Some(&std::path::PathBuf::from("/tmp/samples"))),
        "/tmp/samples"
    );
}
