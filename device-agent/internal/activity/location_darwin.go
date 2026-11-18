package activity

// locate on a Mac: Core Location only answers a signed app the person has allowed, which
// a background agent isn't. The dashboard places the Mac by its internet address instead.
func locate() (*Fix, error) { return nil, ErrNoLocationService }
