//  Stub host app for the UI test bundle.
//
//  XCUITest needs a host application target to hang the test runner off. This
//  one never does anything — all the real code lives in CrossyRunnerUITests,
//  which drives Crossy Road by bundle ID. Do not put logic here.

import SwiftUI

@main
struct CrossyRunnerApp: App {
    var body: some Scene {
        WindowGroup {
            Text("CrossyRunner host — nothing to see here.")
                .padding()
        }
    }
}
