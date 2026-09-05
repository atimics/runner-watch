#[cfg(test)]
mod tests {
    use glib::prelude::*;

    #[test]
    fn forward_and_backward_reads_keep_the_output_pointer() {
        let variant = ["alpha", "", "λ", "omega"].to_variant();
        let mut iter = variant.array_iter_str().unwrap();
        assert_eq!(iter.next(), Some("alpha"));
        assert_eq!(iter.next_back(), Some("omega"));
        assert_eq!(iter.next_back(), Some("λ"));
        assert_eq!(iter.next(), Some(""));
        assert_eq!(iter.next(), None);
        assert_eq!(iter.next_back(), None);
    }

    #[test]
    fn indexed_reads_and_last_keep_the_output_pointer() {
        let variant = ["0", "1", "2", "3", "4", "5"].to_variant();
        let mut iter = variant.array_iter_str().unwrap();
        assert_eq!(iter.nth(1), Some("1"));
        assert_eq!(iter.next(), Some("2"));
        assert_eq!(iter.nth_back(2), Some("3"));
        assert_eq!(iter.next(), None);
        assert_eq!(variant.array_iter_str().unwrap().last(), Some("5"));
    }

    #[test]
    fn empty_and_out_of_bounds_iterators_finish() {
        let empty: [&str; 0] = [];
        let variant = empty.to_variant();
        assert_eq!(variant.array_iter_str().unwrap().next(), None);
        assert_eq!(variant.array_iter_str().unwrap().last(), None);
        let variant = ["value"].to_variant();
        assert_eq!(variant.array_iter_str().unwrap().nth(usize::MAX), None);
        assert_eq!(variant.array_iter_str().unwrap().nth_back(usize::MAX), None);
    }
}

#[cfg(test)]
mod urlpattern_tests {
    use urlpattern::{UrlPattern, UrlPatternInit, UrlPatternMatchInput};

    #[test]
    fn unicode_middle_dot_remains_in_the_group_name() {
        let pattern = <UrlPattern>::parse(
            UrlPatternInit {
                pathname: Some("/:thereisa\u{30FB}middledot.".to_owned()),
                ..Default::default()
            },
            Default::default(),
        )
        .unwrap();
        let matched = pattern
            .exec(UrlPatternMatchInput::Init(UrlPatternInit {
                pathname: Some("/value.".to_owned()),
                ..Default::default()
            }))
            .unwrap()
            .unwrap();
        assert_eq!(
            matched.pathname.groups["thereisa\u{30FB}middledot"],
            Some("value".into())
        );
    }

    #[test]
    fn ascii_url_scopes_preserve_named_groups_and_host_checks() {
        let pattern = <UrlPattern>::parse(
            UrlPatternInit {
                protocol: Some("https".into()),
                hostname: Some("example.com".into()),
                pathname: Some("/coins/:coin_id".into()),
                ..Default::default()
            },
            Default::default(),
        )
        .unwrap();
        let matched = pattern
            .exec(UrlPatternMatchInput::Init(UrlPatternInit {
                protocol: Some("https".into()),
                hostname: Some("example.com".into()),
                pathname: Some("/coins/dogecoin".into()),
                ..Default::default()
            }))
            .unwrap()
            .unwrap();
        assert_eq!(matched.pathname.groups["coin_id"], Some("dogecoin".into()));
        assert!(pattern
            .exec(UrlPatternMatchInput::Init(UrlPatternInit {
                protocol: Some("https".into()),
                hostname: Some("other.example".into()),
                pathname: Some("/coins/dogecoin".into()),
                ..Default::default()
            }))
            .unwrap()
            .is_none());
    }
}
